// Private guest-only acceptance probe. No default endpoint or volume changes.
// cl /nologo /std:c++17 /EHsc /W4 /MT loopback_probe.cpp /link ole32.lib uuid.lib cfgmgr32.lib
#define NOMINMAX
#include <windows.h>
#include <mmdeviceapi.h>
#include <audioclient.h>
#include <functiondiscoverykeys_devpkey.h>
#include <cfgmgr32.h>
#include <mmreg.h>
#include <algorithm>
#include <cmath>
#include <cstdarg>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <cwchar>
#include <stdexcept>
#include <string>

namespace {
constexpr wchar_t kRoot[] = L"ROOT\\MEDIA\\0000";
constexpr wchar_t kHardware[] = L"ROOT\\RemonteVoiceAudio";
constexpr double kTau = 6.2831853071795864769;
constexpr double kFrequency = 900.0;
constexpr double kAmplitude = 0.18;
constexpr GUID kPcm = {1, 0, 0x0010, {0x80, 0, 0, 0xaa, 0, 0x38, 0x9b, 0x71}};
constexpr GUID kFloat = {3, 0, 0x0010, {0x80, 0, 0, 0xaa, 0, 0x38, 0x9b, 0x71}};

class Report {
    FILE* file_ = nullptr;
    size_t remaining_ = 32768;
public:
    Report() {
        const wchar_t* directory = L"C:\\Windows\\Temp\\RemonteVoiceLab-20261025";
        if (!CreateDirectoryW(directory, nullptr) && GetLastError() != ERROR_ALREADY_EXISTS) return;
        const DWORD attributes = GetFileAttributesW(directory);
        if (attributes == INVALID_FILE_ATTRIBUTES || !(attributes & FILE_ATTRIBUTE_DIRECTORY)) return;
        _wfopen_s(&file_, L"C:\\Windows\\Temp\\RemonteVoiceLab-20261025\\loopback-probe.txt", L"w");
    }
    ~Report() { if (file_) fclose(file_); }
    explicit operator bool() const { return file_ != nullptr; }
    void log(const char* format, ...) {
        if (!file_ || !remaining_) return;
        char line[1024] = {};
        va_list args;
        va_start(args, format);
        _vsnprintf_s(line, sizeof(line), _TRUNCATE, format, args);
        va_end(args);
        const size_t size = std::min(remaining_, strlen(line));
        fwrite(line, 1, size, file_);
        remaining_ -= size;
        fflush(file_);
    }
};

template<class T> struct Com {
    T* pointer = nullptr;
    Com() = default;
    Com(const Com&) = delete;
    Com& operator=(const Com&) = delete;
    ~Com() { if (pointer) pointer->Release(); }
    T* operator->() const { return pointer; }
    T** out() { return &pointer; }
};

void check(HRESULT result, const char* operation, Report& report) {
    if (FAILED(result)) {
        report.log("%s HRESULT=0x%08lx\n", operation, static_cast<unsigned long>(result));
        throw std::runtime_error(operation);
    }
}

bool remote_voice_root(DEVINST node) {
    wchar_t hardware[4096] = {};
    ULONG type = 0, bytes = sizeof(hardware);
    if (CM_Get_DevNode_Registry_PropertyW(node, CM_DRP_HARDWAREID, &type,
            hardware, &bytes, 0) != CR_SUCCESS || type != REG_MULTI_SZ ||
        bytes < 2 * sizeof(wchar_t) || bytes > sizeof(hardware) || bytes % sizeof(wchar_t)) return false;
    const size_t count = bytes / sizeof(wchar_t);
    if (hardware[count - 1] || hardware[count - 2]) return false;
    for (size_t offset = 0; offset < count - 1 && hardware[offset];) {
        const size_t length = wcsnlen_s(hardware + offset, count - offset);
        if (length == count - offset) return false;
        if (_wcsicmp(hardware + offset, kHardware) == 0) return true;
        offset += length + 1;
    }
    return false;
}

bool verified_ancestor(const wchar_t* instance, unsigned& depth) {
    if (!instance || !*instance || wcsnlen_s(instance, MAX_DEVICE_ID_LEN) == MAX_DEVICE_ID_LEN) return false;
    DEVINST node = 0;
    if (CM_Locate_DevNodeW(&node, const_cast<wchar_t*>(instance), CM_LOCATE_DEVNODE_NORMAL) != CR_SUCCESS) return false;
    for (depth = 0; depth < 32; ++depth) {
        wchar_t id[MAX_DEVICE_ID_LEN] = {};
        if (CM_Get_Device_IDW(node, id, MAX_DEVICE_ID_LEN, 0) != CR_SUCCESS) return false;
        if (_wcsicmp(id, kRoot) == 0) return remote_voice_root(node);
        DEVINST parent = 0;
        if (CM_Get_Parent(&parent, node, 0) != CR_SUCCESS || parent == node) return false;
        node = parent;
    }
    return false;
}

void select_render(IMMDeviceEnumerator* enumerator, Com<IMMDevice>& selected, Report& report) {
    Com<IMMDeviceCollection> devices;
    check(enumerator->EnumAudioEndpoints(eRender, DEVICE_STATE_ACTIVE, devices.out()), "EnumAudioEndpoints(render)", report);
    UINT count = 0, matches = 0;
    check(devices->GetCount(&count), "GetCount", report);
    if (count > 256) throw std::runtime_error("Render endpoint count exceeds probe bound");
    for (UINT index = 0; index < count; ++index) {
        Com<IMMDevice> candidate;
        Com<IPropertyStore> properties;
        check(devices->Item(index, candidate.out()), "Endpoint Item", report);
        check(candidate->OpenPropertyStore(STGM_READ, properties.out()), "OpenPropertyStore", report);
        PROPVARIANT instance;
        PropVariantInit(&instance);
        const HRESULT result = properties->GetValue(PKEY_Device_InstanceId, &instance);
        std::wstring nodeInstance;
        if (SUCCEEDED(result) && instance.vt == VT_LPWSTR && instance.pwszVal &&
            wcsnlen_s(instance.pwszVal, MAX_DEVICE_ID_LEN) < MAX_DEVICE_ID_LEN)
            nodeInstance = instance.pwszVal;
        const bool missingProperty = FAILED(result) || instance.vt == VT_EMPTY || instance.vt == VT_NULL;
        PropVariantClear(&instance);
        bool fromEndpointId = false;
        if (nodeInstance.empty() && missingProperty) {
            // This guest exposes VT_EMPTY for PKEY_Device_InstanceId. Resolve the
            // endpoint's canonical MMDevice ID to its existing SWD PnP node.
            struct EndpointId {
                wchar_t* pointer = nullptr;
                ~EndpointId() { CoTaskMemFree(pointer); }
            } id;
            check(candidate->GetId(&id.pointer), "GetId for PnP identity", report);
            constexpr wchar_t prefix[] = L"{0.0.0.00000000}.";
            constexpr size_t prefixLength = sizeof(prefix) / sizeof(wchar_t) - 1;
            const size_t length = id.pointer ? wcsnlen_s(id.pointer, MAX_DEVICE_ID_LEN) : 0;
            GUID endpointGuid = {};
            if (length == prefixLength + 38 && wcsncmp(id.pointer, prefix, prefixLength) == 0 &&
                id.pointer[prefixLength] == L'{' && id.pointer[length - 1] == L'}' &&
                SUCCEEDED(CLSIDFromString(id.pointer + prefixLength, &endpointGuid))) {
                nodeInstance = L"SWD\\MMDEVAPI\\";
                nodeInstance += id.pointer;
                fromEndpointId = true;
            }
        }
        unsigned depth = 0;
        const bool verified = !nodeInstance.empty() && verified_ancestor(nodeInstance.c_str(), depth);
        if (verified) {
            ++matches;
            report.log("verified render identitySource=%s instance=%.*ls ancestorDepth=%u root=%ls hardware=%ls\n",
                fromEndpointId ? "GetId/SWD" : "PKEY_Device_InstanceId",
                MAX_DEVICE_ID_LEN - 1, nodeInstance.c_str(), depth, kRoot, kHardware);
            if (!selected.pointer) {
                selected.pointer = candidate.pointer;
                candidate.pointer = nullptr;
            }
        }
    }
    report.log("activeRenderEndpoints=%u verifiedMatches=%u\n", count, matches);
    if (matches != 1) throw std::runtime_error("Expected exactly one render endpoint with the verified lab root ancestor");
}

struct Format {
    UINT32 rate = 0;
    WORD channels = 0, bytes = 0, bits = 0, validBits = 0;
    bool floating = false;
};

Format validate_format(const WAVEFORMATEX* wave) {
    if (!wave) throw std::runtime_error("Missing mix format");
    Format format;
    format.rate = wave->nSamplesPerSec;
    format.channels = wave->nChannels;
    format.bits = wave->wBitsPerSample;
    format.validBits = format.bits;
    if (wave->wFormatTag == WAVE_FORMAT_EXTENSIBLE) {
        if (wave->cbSize < sizeof(WAVEFORMATEXTENSIBLE) - sizeof(WAVEFORMATEX))
            throw std::runtime_error("Truncated extensible format");
        const auto* extended = reinterpret_cast<const WAVEFORMATEXTENSIBLE*>(wave);
        format.validBits = extended->Samples.wValidBitsPerSample;
        if (IsEqualGUID(extended->SubFormat, kFloat)) format.floating = true;
        else if (!IsEqualGUID(extended->SubFormat, kPcm)) throw std::runtime_error("Unsupported extensible subtype");
        DWORD mask = extended->dwChannelMask;
        unsigned maskChannels = 0;
        for (DWORD value = mask; value; value &= value - 1) ++maskChannels;
        if (mask && maskChannels != format.channels) throw std::runtime_error("Invalid channel mask");
    } else if (wave->wFormatTag == WAVE_FORMAT_IEEE_FLOAT) format.floating = true;
    else if (wave->wFormatTag != WAVE_FORMAT_PCM) throw std::runtime_error("Unsupported mix format tag");
    if (!format.channels || format.channels > 8 || format.rate < 8000 || format.rate > 192000 ||
        (format.bits != 16 && format.bits != 32) || format.validBits < 2 || format.validBits > format.bits ||
        (format.floating && (format.bits != 32 || format.validBits != 32)))
        throw std::runtime_error("Unsupported or invalid mix format");
    format.bytes = static_cast<WORD>(format.bits / 8);
    if (wave->nBlockAlign != format.channels * format.bytes ||
        wave->nAvgBytesPerSec != format.rate * wave->nBlockAlign)
        throw std::runtime_error("Inconsistent mix format alignment/rate");
    return format;
}

void write_sample(BYTE* destination, double value, const Format& format) {
    if (format.floating) {
        const float sample = static_cast<float>(value);
        memcpy(destination, &sample, sizeof(sample));
    } else {
        // Extensible PCM valid bits are left aligned in the sample container.
        const int64_t quantized = static_cast<int64_t>(std::llround(value * std::ldexp(1.0, format.validBits - 1)));
        const int64_t container = quantized * (int64_t{1} << (format.bits - format.validBits));
        if (format.bits == 16) {
            const int16_t sample = static_cast<int16_t>(container);
            memcpy(destination, &sample, sizeof(sample));
        } else {
            const int32_t sample = static_cast<int32_t>(container);
            memcpy(destination, &sample, sizeof(sample));
        }
    }
}

double read_sample(const BYTE* source, const Format& format) {
    if (format.floating) {
        float sample;
        memcpy(&sample, source, sizeof(sample));
        if (!std::isfinite(sample)) throw std::runtime_error("Non-finite loopback sample");
        return sample;
    }
    if (format.bits == 16) {
        int16_t sample;
        memcpy(&sample, source, sizeof(sample));
        return sample / 32768.0;
    }
    int32_t sample;
    memcpy(&sample, source, sizeof(sample));
    return sample / 2147483648.0;
}

struct Stats {
    UINT64 frames = 0, signalFrames = 0, packets = 0;
    unsigned discontinuities = 0, timestampErrors = 0;
    double squares = 0, monoSquares = 0, peak = 0, sine = 0, cosine = 0;
    double rms(const Format& format) const { return frames ? std::sqrt(squares / (frames * format.channels)) : 0; }
    double tone_fraction() const {
        return frames && monoSquares ? 2 * (sine * sine + cosine * cosine) / (frames * monoSquares) : 0;
    }
};

void drain(IAudioCaptureClient* capture, const Format& format, UINT32 bufferFrames,
           Stats& stats, bool& seenPacket, Report& report) {
    for (unsigned packet = 0; packet < 256; ++packet) {
        UINT32 available = 0;
        check(capture->GetNextPacketSize(&available), "GetNextPacketSize", report);
        if (!available) return;
        BYTE* data = nullptr;
        UINT32 frames = 0;
        DWORD flags = 0;
        UINT64 position = 0;
        check(capture->GetBuffer(&data, &frames, &flags, &position, nullptr), "Capture GetBuffer", report);
        try {
            if (!frames || frames > bufferFrames || (!(flags & AUDCLNT_BUFFERFLAGS_SILENT) && !data))
                throw std::runtime_error("Invalid loopback packet");
            if (seenPacket && (flags & AUDCLNT_BUFFERFLAGS_DATA_DISCONTINUITY)) ++stats.discontinuities;
            if (flags & AUDCLNT_BUFFERFLAGS_TIMESTAMP_ERROR) ++stats.timestampErrors;
            seenPacket = true;
            for (UINT32 frame = 0; frame < frames; ++frame) {
                double mono = 0, framePeak = 0;
                for (WORD channel = 0; channel < format.channels; ++channel) {
                    const double sample = (flags & AUDCLNT_BUFFERFLAGS_SILENT) ? 0 :
                        read_sample(data + (static_cast<size_t>(frame) * format.channels + channel) * format.bytes, format);
                    stats.squares += sample * sample;
                    mono += sample / format.channels;
                    framePeak = std::max(framePeak, std::fabs(sample));
                }
                stats.peak = std::max(stats.peak, framePeak);
                if (framePeak > 0.015) ++stats.signalFrames;
                stats.monoSquares += mono * mono;
                const double phase = kTau * kFrequency * ((position + frame) % format.rate) / format.rate;
                stats.sine += mono * std::sin(phase);
                stats.cosine += mono * std::cos(phase);
            }
            stats.frames += frames;
            ++stats.packets;
        } catch (...) {
            capture->ReleaseBuffer(frames);
            throw;
        }
        check(capture->ReleaseBuffer(frames), "Capture ReleaseBuffer", report);
    }
    throw std::runtime_error("Loopback packet drain exceeded bound");
}

void fill(IAudioClient* client, IAudioRenderClient* render, UINT32 bufferFrames,
          const Format& format, UINT64& toneQueued, ULONGLONG& quietAt, Report& report) {
    UINT32 padding = 0;
    check(client->GetCurrentPadding(&padding), "GetCurrentPadding", report);
    if (padding > bufferFrames) throw std::runtime_error("Invalid render padding");
    const UINT32 available = bufferFrames - padding;
    if (!available) return;
    BYTE* data = nullptr;
    check(render->GetBuffer(available, &data), "Render GetBuffer", report);
    if (!data) {
        render->ReleaseBuffer(available, AUDCLNT_BUFFERFLAGS_SILENT);
        throw std::runtime_error("Missing render buffer");
    }
    for (UINT32 frame = 0; frame < available; ++frame) {
        double sample = 0;
        if (toneQueued < format.rate) {
            sample = kAmplitude * std::sin(kTau * kFrequency * toneQueued / format.rate);
            ++toneQueued;
            if (toneQueued == format.rate)
                quietAt = GetTickCount64() + (1000ULL * bufferFrames + format.rate - 1) / format.rate + 250;
        }
        for (WORD channel = 0; channel < format.channels; ++channel)
            write_sample(data + (static_cast<size_t>(frame) * format.channels + channel) * format.bytes, sample, format);
    }
    check(render->ReleaseBuffer(available, 0), "Render ReleaseBuffer", report);
}

void run(Report& report) {
    Com<IMMDeviceEnumerator> enumerator;
    Com<IMMDevice> endpoint;
    Com<IAudioClient> renderClient, captureClient;
    Com<IAudioRenderClient> render;
    Com<IAudioCaptureClient> capture;
    check(CoCreateInstance(__uuidof(MMDeviceEnumerator), nullptr, CLSCTX_ALL,
        __uuidof(IMMDeviceEnumerator), reinterpret_cast<void**>(enumerator.out())), "Create enumerator", report);
    select_render(enumerator.pointer, endpoint, report);
    check(endpoint->Activate(__uuidof(IAudioClient), CLSCTX_ALL, nullptr,
        reinterpret_cast<void**>(renderClient.out())), "Activate render", report);
    check(endpoint->Activate(__uuidof(IAudioClient), CLSCTX_ALL, nullptr,
        reinterpret_cast<void**>(captureClient.out())), "Activate loopback", report);
    struct MixFormat {
        WAVEFORMATEX* pointer = nullptr;
        ~MixFormat() { CoTaskMemFree(pointer); }
    } mix;
    check(renderClient->GetMixFormat(&mix.pointer), "GetMixFormat", report);
    const Format format = validate_format(mix.pointer);
    report.log("mix rate=%u channels=%u bits=%u validBits=%u encoding=%s\n",
        format.rate, static_cast<unsigned>(format.channels), static_cast<unsigned>(format.bits),
        static_cast<unsigned>(format.validBits), format.floating ? "FLOAT" : "PCM");
    check(renderClient->Initialize(AUDCLNT_SHAREMODE_SHARED, 0, 1000000, 0, mix.pointer, nullptr), "Initialize render shared", report);
    check(captureClient->Initialize(AUDCLNT_SHAREMODE_SHARED, AUDCLNT_STREAMFLAGS_LOOPBACK,
        1000000, 0, mix.pointer, nullptr), "Initialize shared loopback", report);
    check(renderClient->GetService(__uuidof(IAudioRenderClient), reinterpret_cast<void**>(render.out())), "Get render service", report);
    check(captureClient->GetService(__uuidof(IAudioCaptureClient), reinterpret_cast<void**>(capture.out())), "Get loopback service", report);
    UINT32 renderFrames = 0, captureFrames = 0;
    check(renderClient->GetBufferSize(&renderFrames), "Render buffer size", report);
    check(captureClient->GetBufferSize(&captureFrames), "Loopback buffer size", report);
    if (!renderFrames || !captureFrames || renderFrames > format.rate / 2 || captureFrames > 2 * format.rate)
        throw std::runtime_error("Unexpected audio buffer size");
    report.log("renderBufferFrames=%u captureBufferFrames=%u toneHz=900 amplitude=0.18 toneFrames=%u\n",
        renderFrames, captureFrames, format.rate);
    // Stop before COM releases on every success/failure path after initialization.
    struct StopClients {
        IAudioClient* render;
        IAudioClient* capture;
        ~StopClients() { render->Stop(); capture->Stop(); }
    } stop{renderClient.pointer, captureClient.pointer};
    UINT64 toneQueued = 0;
    ULONGLONG quietAt = 0;
    fill(renderClient.pointer, render.pointer, renderFrames, format, toneQueued, quietAt, report);
    check(captureClient->Start(), "Start loopback", report);
    check(renderClient->Start(), "Start render", report);
    Stats tone, silence;
    bool seenPacket = false, quiet = false;
    ULONGLONG quietStarted = 0;
    const ULONGLONG deadline = GetTickCount64() + 5000;
    for (;;) {
        if (GetTickCount64() >= deadline) throw std::runtime_error("Five-second probe deadline exceeded");
        fill(renderClient.pointer, render.pointer, renderFrames, format, toneQueued, quietAt, report);
        drain(capture.pointer, format, captureFrames, quiet ? silence : tone, seenPacket, report);
        const ULONGLONG now = GetTickCount64();
        if (!quiet && quietAt && now >= quietAt) { quiet = true; quietStarted = now; }
        if (quiet && now - quietStarted >= 300 && silence.frames >= format.rate / 5) break;
        Sleep(2);
    }
    report.log("tone queued=%llu capturedFrames=%llu signalFrames=%llu packets=%llu rms=%.6f peak=%.6f fraction900Hz=%.6f discontinuities=%u timestampErrors=%u\n",
        toneQueued, tone.frames, tone.signalFrames, tone.packets, tone.rms(format), tone.peak,
        tone.tone_fraction(), tone.discontinuities, tone.timestampErrors);
    report.log("zeroRender capturedFrames=%llu packets=%llu rms=%.6f peak=%.6f discontinuities=%u timestampErrors=%u\n",
        silence.frames, silence.packets, silence.rms(format), silence.peak, silence.discontinuities, silence.timestampErrors);
    if (toneQueued != format.rate || tone.frames < format.rate * 7ULL / 10 ||
        tone.signalFrames < format.rate * 6ULL / 10 || tone.rms(format) < 0.02 ||
        tone.rms(format) > 0.30 || tone.peak > 0.35 || tone.tone_fraction() < 0.50 ||
        tone.discontinuities || tone.timestampErrors || silence.discontinuities || silence.timestampErrors ||
        silence.frames < format.rate / 5 || silence.rms(format) > 0.001 || silence.peak > 0.002)
        throw std::runtime_error("Synthetic render/loopback acceptance thresholds failed");
    // This probe checks zero-render silence only; process-death/exclusive ownership are separate tests.
    report.log("PASS: verified virtual render -> shared WASAPI loopback, 900Hz signal and silence while rendering zeros.\n");
}
} // namespace

int main() {
    Report report;
    if (!report) return 1;
    report.log("RemonteVoice private shared render/loopback probe; report capped at 32768 bytes.\n");
    const HRESULT initialized = CoInitializeEx(nullptr, COINIT_MULTITHREADED);
    if (FAILED(initialized)) {
        report.log("FAIL: CoInitializeEx HRESULT=0x%08lx\n", static_cast<unsigned long>(initialized));
        return 1;
    }
    int result = 1;
    try { run(report); result = 0; }
    catch (const std::exception& error) { report.log("FAIL: %s\n", error.what()); }
    catch (...) { report.log("FAIL: unexpected exception\n"); }
    CoUninitialize();
    return result;
}
