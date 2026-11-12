// Private VM synthetic test. Opens only the verified RemonteVoice lab microphone.
#define NOMINMAX
#include <windows.h>
#include <mmdeviceapi.h>
#include <audioclient.h>
#include <functiondiscoverykeys_devpkey.h>
#include <setupapi.h>
#include <cfgmgr32.h>
#include <ks.h>
#include <mmreg.h>
#include <cstdio>
#include <cmath>
#include <cwchar>
#include <cstdint>
#include <vector>
#include <string>
#include <stdexcept>
#pragma comment(lib, "cfgmgr32.lib")

static FILE* report;
static const wchar_t* lab_root = L"ROOT\\MEDIA\\0000";
static const double tau = 6.283185307179586;
static void hr(HRESULT h) {
    if (FAILED(h)) {
        fprintf(report, "HRESULT=0x%08lx\n", h);
        throw std::runtime_error("COM failure");
    }
}
static void win32_failure(const char* operation, DWORD error) {
    fprintf(report, "%s error=%lu\n", operation, error);
    throw std::runtime_error(operation);
}
struct Message {
    ULONG version, command;
    ULONGLONG session;
    BYTE nonce[16];
    ULONG size, reserved;
    short pcm[480];
};
static_assert(sizeof(Message) == 1000, "wire ABI");

static bool endpoint_is_lab_root(IMMDevice* device, IPropertyStore* properties) {
    PROPVARIANT instance;
    PropVariantInit(&instance);
    HRESULT status = properties->GetValue(PKEY_Device_InstanceId, &instance);
    std::wstring id;
    if (SUCCEEDED(status) && instance.vt == VT_LPWSTR && instance.pwszVal && *instance.pwszVal) {
        id = instance.pwszVal;
    } else {
        fprintf(report, "endpoint instance property unavailable: hr=0x%08lx vt=%u; resolving MMDevice ID\n", status, instance.vt);
        LPWSTR endpointId = nullptr;
        HRESULT idStatus = device->GetId(&endpointId);
        if (SUCCEEDED(idStatus) && endpointId && *endpointId) id = std::wstring(L"SWD\\MMDEVAPI\\") + endpointId;
        if (endpointId) CoTaskMemFree(endpointId);
        if (id.empty()) {
            fprintf(report, "endpoint GetId HRESULT=0x%08lx\n", idStatus);
            PropVariantClear(&instance);
            return false;
        }
    }
    PropVariantClear(&instance);
    DEVINST node = 0;
    CONFIGRET cr = CM_Locate_DevNodeW(&node, &id[0], CM_LOCATE_DEVNODE_NORMAL);
    if (cr != CR_SUCCESS) {
        fprintf(report, "CM_Locate_DevNode instance=%ls cr=%lu\n", id.c_str(), cr);
        return false;
    }
    for (unsigned depth = 0; depth < 32; ++depth) {
        wchar_t current[MAX_DEVICE_ID_LEN] = {};
        cr = CM_Get_Device_IDW(node, current, MAX_DEVICE_ID_LEN, 0);
        if (cr != CR_SUCCESS) {
            fprintf(report, "CM_Get_Device_ID cr=%lu\n", cr);
            return false;
        }
        fprintf(report, "endpoint ancestor[%u]=%ls\n", depth, current);
        if (!_wcsicmp(current, lab_root)) {
            wchar_t hardwareIds[32768] = {};
            ULONG type = 0, bytes = sizeof(hardwareIds);
            cr = CM_Get_DevNode_Registry_PropertyW(node, CM_DRP_HARDWAREID, &type, hardwareIds, &bytes, 0);
            if (cr != CR_SUCCESS || type != REG_MULTI_SZ || bytes > sizeof(hardwareIds) - sizeof(wchar_t)) {
                fprintf(report, "endpoint root hardware ID invalid: cr=%lu type=%lu bytes=%lu\n", cr, type, bytes);
                return false;
            }
            for (const wchar_t* value = hardwareIds; *value; value += wcslen(value) + 1) {
                if (!_wcsicmp(value, L"ROOT\\RemonteVoiceAudio")) {
                    fprintf(report, "endpoint root hardware ID verified=%ls\n", value);
                    return true;
                }
            }
            return false;
        }
        DEVINST parent = 0;
        cr = CM_Get_Parent(&parent, node, 0);
        if (cr != CR_SUCCESS) return false;
        node = parent;
    }
    return false;
}

static HANDLE open_filter() {
    GUID category = {0x6994ad04, 0x93ef, 0x11d0, {0xa3, 0xcc, 0, 0xa0, 0xc9, 0x22, 0x31, 0x96}};
    HDEVINFO set = SetupDiGetClassDevsW(&category, nullptr, nullptr, DIGCF_PRESENT | DIGCF_DEVICEINTERFACE);
    if (set == INVALID_HANDLE_VALUE) win32_failure("SetupDiGetClassDevs", GetLastError());
    std::wstring selected;
    unsigned matches = 0;
    try {
        for (DWORD i = 0;; ++i) {
            SP_DEVICE_INTERFACE_DATA face = {sizeof(face)};
            if (!SetupDiEnumDeviceInterfaces(set, nullptr, &category, i, &face)) {
                DWORD error = GetLastError();
                if (error != ERROR_NO_MORE_ITEMS) win32_failure("SetupDiEnumDeviceInterfaces", error);
                break;
            }
            DWORD bytes = 0;
            SetupDiGetDeviceInterfaceDetailW(set, &face, nullptr, 0, &bytes, nullptr);
            if (bytes < 8 || bytes > 65536) throw std::runtime_error("Invalid interface detail size");
            std::vector<BYTE> data(bytes);
            auto detail = reinterpret_cast<SP_DEVICE_INTERFACE_DETAIL_DATA_W*>(data.data());
            detail->cbSize = sizeof(*detail);
            SP_DEVINFO_DATA dev = {sizeof(dev)};
            if (!SetupDiGetDeviceInterfaceDetailW(set, &face, detail, bytes, nullptr, &dev))
                win32_failure("SetupDiGetDeviceInterfaceDetail", GetLastError());
            std::wstring path = detail->DevicePath;
            const wchar_t* suffix = L"\\wavemicarray1";
            if (path.size() < wcslen(suffix) || _wcsicmp(path.c_str() + path.size() - wcslen(suffix), suffix)) continue;
            wchar_t instance[MAX_DEVICE_ID_LEN] = {};
            if (!SetupDiGetDeviceInstanceIdW(set, &dev, instance, MAX_DEVICE_ID_LEN, nullptr))
                win32_failure("SetupDiGetDeviceInstanceId", GetLastError());
            if (_wcsicmp(instance, lab_root)) continue;
            wchar_t ids[32768] = {};
            DWORD type = 0, required = 0;
            if (!SetupDiGetDeviceRegistryPropertyW(set, &dev, SPDRP_HARDWAREID, &type,
                reinterpret_cast<BYTE*>(ids), sizeof(ids), &required))
                win32_failure("SetupDiGetDeviceRegistryProperty", GetLastError());
            if (type != REG_MULTI_SZ || required > sizeof(ids) - sizeof(wchar_t))
                throw std::runtime_error("Invalid hardware ID property");
            for (const wchar_t* p = ids; *p; p += wcslen(p) + 1) {
                if (!_wcsicmp(p, L"ROOT\\RemonteVoiceAudio")) {
                    selected = path;
                    ++matches;
                    break;
                }
            }
        }
    } catch (...) {
        SetupDiDestroyDeviceInfoList(set);
        throw;
    }
    SetupDiDestroyDeviceInfoList(set);
    if (matches != 1) throw std::runtime_error("Expected exactly one verified lab virtual mic filter");
    fprintf(report, "KS filter=%ls root=%ls\n", selected.c_str(), lab_root);
    HANDLE handle = CreateFileW(selected.c_str(), GENERIC_READ | GENERIC_WRITE,
        FILE_SHARE_READ | FILE_SHARE_WRITE, nullptr, OPEN_EXISTING, 0, nullptr);
    if (handle == INVALID_HANDLE_VALUE) win32_failure("CreateFile(filter)", GetLastError());
    return handle;
}

struct KsResult { bool ok; DWORD error; };
static KsResult send(HANDLE h, Message& m, ULONG command) {
    KSPROPERTY prop = {};
    prop.Set = {0xee47bfaa, 0x9650, 0x41d5, {0xaf, 0xa0, 0x09, 0xce, 0x9f, 0x32, 0xe7, 0x1b}};
    prop.Id = 0;
    prop.Flags = KSPROPERTY_TYPE_SET;
    m.command = command;
    m.size = command == 2 ? 960 : 0;
    DWORD returned = 0;
    BOOL ok = DeviceIoControl(h, 0x002f0003, &prop, sizeof(prop), &m, sizeof(m), &returned, nullptr);
    DWORD error = ok ? ERROR_SUCCESS : GetLastError();
    return {ok != FALSE, error};
}
static void require_send(HANDLE h, Message& m, ULONG command) {
    KsResult result = send(h, m, command);
    if (!result.ok) {
        fprintf(report, "KS command=%lu error=%lu\n", command, result.error);
        throw std::runtime_error("KS command rejected");
    }
}
static void require_denied(HANDLE h, Message& m, ULONG command, DWORD expected) {
    KsResult result = send(h, m, command);
    fprintf(report, "expected rejection: command=%lu ok=%u error=%lu expected=%lu\n",
        command, result.ok ? 1 : 0, result.error, expected);
    if (result.ok || result.error != expected) throw std::runtime_error("Unexpected KS ownership result");
}

enum class SampleKind { Pcm16, Pcm32, Float32 };
static SampleKind sample_kind(WAVEFORMATEX* format) {
    WORD tag = format->wFormatTag;
    unsigned valid = format->wBitsPerSample;
    if (tag == WAVE_FORMAT_EXTENSIBLE) {
        if (format->cbSize < sizeof(WAVEFORMATEXTENSIBLE) - sizeof(WAVEFORMATEX))
            throw std::runtime_error("Short extensible mix format");
        auto extended = reinterpret_cast<WAVEFORMATEXTENSIBLE*>(format);
        const GUID pcm = {1, 0, 0x10, {0x80, 0, 0, 0xaa, 0, 0x38, 0x9b, 0x71}};
        const GUID floating = {3, 0, 0x10, {0x80, 0, 0, 0xaa, 0, 0x38, 0x9b, 0x71}};
        if (IsEqualGUID(extended->SubFormat, pcm)) tag = WAVE_FORMAT_PCM;
        else if (IsEqualGUID(extended->SubFormat, floating)) tag = WAVE_FORMAT_IEEE_FLOAT;
        else throw std::runtime_error("Unsupported extensible mix subtype");
        valid = extended->Samples.wValidBitsPerSample;
        fprintf(report, "mix subtype=%lu validBits=%u channelMask=0x%lx\n",
            extended->SubFormat.Data1, valid, extended->dwChannelMask);
    }
    fprintf(report, "mix: channels=%u rate=%lu bits=%u valid=%u tag=%u blockAlign=%u\n",
        format->nChannels, format->nSamplesPerSec, format->wBitsPerSample, valid, format->wFormatTag, format->nBlockAlign);
    if (format->nSamplesPerSec != 48000 || !format->nChannels || format->nChannels > 8 ||
        !valid || valid > format->wBitsPerSample ||
        format->nBlockAlign != format->nChannels * (format->wBitsPerSample / 8) ||
        format->nAvgBytesPerSec != format->nSamplesPerSec * format->nBlockAlign)
        throw std::runtime_error("Unexpected mix format geometry");
    if (tag == WAVE_FORMAT_IEEE_FLOAT && format->wBitsPerSample == 32 && valid == 32) return SampleKind::Float32;
    if (tag == WAVE_FORMAT_PCM && format->wBitsPerSample == 16) return SampleKind::Pcm16;
    if (tag == WAVE_FORMAT_PCM && format->wBitsPerSample == 32) return SampleKind::Pcm32;
    throw std::runtime_error("Unsupported mix sample encoding");
}

struct Stats {
    UINT64 frames = 0, nonzero = 0, samples = 0;
    unsigned discontinuities = 0, timestampErrors = 0;
    double peak = 0, squares = 0;
    // Independent 10 ms correlations tolerate phase changes at packet boundaries.
    unsigned windowFrames = 0;
    double windowSin = 0, windowCos = 0, windowSquares = 0;
    double toneEnergy = 0, analyzedEnergy = 0;
    void frame(double first, double framePeak, double frameSquares, unsigned channels) {
        ++frames;
        samples += channels;
        squares += frameSquares;
        if (framePeak > peak) peak = framePeak;
        if (framePeak > 0.001) ++nonzero;
        double phase = tau * 900 * windowFrames / 48000.0;
        windowSin += first * std::sin(phase);
        windowCos += first * std::cos(phase);
        windowSquares += first * first;
        if (++windowFrames == 480) {
            toneEnergy += 2 * (windowSin * windowSin + windowCos * windowCos) / 480.0;
            analyzedEnergy += windowSquares;
            windowFrames = 0;
            windowSin = windowCos = windowSquares = 0;
        }
    }
    double rms() const { return samples ? std::sqrt(squares / samples) : 0; }
    double tone_ratio() const { return analyzedEnergy ? toneEnergy / analyzedEnergy : 0; }
};
static void drain(IAudioCaptureClient* capture, WAVEFORMATEX* format, SampleKind kind, Stats& stats) {
    UINT32 available = 0;
    hr(capture->GetNextPacketSize(&available));
    while (available) {
        BYTE* data = nullptr;
        UINT32 count = 0;
        DWORD flags = 0;
        hr(capture->GetBuffer(&data, &count, &flags, nullptr, nullptr));
        bool invalid = false;
        if (flags & AUDCLNT_BUFFERFLAGS_DATA_DISCONTINUITY) ++stats.discontinuities;
        if (flags & AUDCLNT_BUFFERFLAGS_TIMESTAMP_ERROR) ++stats.timestampErrors;
        for (UINT32 frame = 0; frame < count; ++frame) {
            double first = 0, peak = 0, squares = 0;
            for (unsigned channel = 0; channel < format->nChannels; ++channel) {
                double value = 0;
                if (!(flags & AUDCLNT_BUFFERFLAGS_SILENT)) {
                    if (!data) { invalid = true; break; }
                    size_t index = static_cast<size_t>(frame) * format->nChannels + channel;
                    if (kind == SampleKind::Float32) value = reinterpret_cast<const float*>(data)[index];
                    else if (kind == SampleKind::Pcm16) value = reinterpret_cast<const int16_t*>(data)[index] / 32768.0;
                    else value = reinterpret_cast<const int32_t*>(data)[index] / 2147483648.0;
                    if (!std::isfinite(value)) { invalid = true; break; }
                }
                if (!channel) first = value;
                if (std::fabs(value) > peak) peak = std::fabs(value);
                squares += value * value;
            }
            if (invalid) break;
            stats.frame(first, peak, squares, format->nChannels);
        }
        hr(capture->ReleaseBuffer(count));
        if (invalid) throw std::runtime_error("Invalid capture buffer or non-finite sample");
        hr(capture->GetNextPacketSize(&available));
    }
}
static void collect(IAudioCaptureClient* capture, WAVEFORMATEX* format, SampleKind kind, Stats& stats, unsigned ms) {
    ULONGLONG until = GetTickCount64() + ms;
    do {
        drain(capture, format, kind, stats);
        Sleep(1);
    } while (GetTickCount64() < until);
    drain(capture, format, kind, stats);
}
static void print_stats(const char* phase, const Stats& stats) {
    fprintf(report, "%s: frames=%llu nonzeroFrames=%llu peak=%.6f rms=%.6f tone900Ratio=%.6f discontinuities=%u timestampErrors=%u\n",
        phase, stats.frames, stats.nonzero, stats.peak, stats.rms(), stats.tone_ratio(), stats.discontinuities, stats.timestampErrors);
    fflush(report);
}
static void require_silence(const char* phase, const Stats& stats) {
    print_stats(phase, stats);
    if (stats.frames < 4000 || stats.peak > 0.001) throw std::runtime_error("Silence acceptance failed");
}
static void require_tone(const char* phase, const Stats& stats, UINT64 minimumFrames) {
    print_stats(phase, stats);
    if (stats.frames < minimumFrames || stats.nonzero < minimumFrames * 2 / 3 ||
        stats.peak < 0.02 || stats.peak > 0.8 || stats.rms() < 0.04 || stats.rms() > 0.4 || stats.tone_ratio() < 0.45)
        throw std::runtime_error("Injected 900 Hz signal acceptance failed");
}
static void tone(HANDLE filter, Message& message, IAudioCaptureClient* capture,
    WAVEFORMATEX* format, SampleKind kind, Stats& stats, unsigned packets) {
    unsigned written = 0, busy = 0;
    ULONGLONG start = GetTickCount64();
    for (unsigned packet = 0; packet < packets; ++packet) {
        for (unsigned j = 0; j < 480; ++j)
            message.pcm[j] = static_cast<short>(6000 * std::sin(tau * 900 * (packet * 480 + j) / 48000.0));
        KsResult result = send(filter, message, 2);
        if (result.ok) ++written;
        else if (result.error == ERROR_BUSY) ++busy;
        else win32_failure("KS WRITE", result.error);
        ULONGLONG due = start + (packet + 1) * 10;
        while (GetTickCount64() < due) {
            drain(capture, format, kind, stats);
            Sleep(1);
        }
    }
    drain(capture, format, kind, stats);
    fprintf(report, "KS tone: requested=%u written=%u busy=%u\n", packets, written, busy);
    if (written < packets * 8 / 10) throw std::runtime_error("Too many rejected tone writes");
}

int main() {
    fopen_s(&report, "C:\\Windows\\Temp\\RemonteVoiceLab-20261025\\pcm-probe.txt", "w");
    if (!report) return 3;
    HRESULT initialized = CoInitializeEx(nullptr, COINIT_MULTITHREADED);
    if (FAILED(initialized)) { fprintf(report, "CoInitializeEx HRESULT=0x%08lx\n", initialized); fclose(report); return 4; }
    HANDLE filter = INVALID_HANDLE_VALUE;
    IMMDeviceEnumerator* en = nullptr;
    IMMDeviceCollection* devices = nullptr;
    IMMDevice* selected = nullptr;
    IAudioClient* client = nullptr;
    IAudioCaptureClient* capture = nullptr;
    WAVEFORMATEX* format = nullptr;
    Message message = {};
    message.version = 1;
    message.session = 0x1122334455667788ULL;
    message.nonce[0] = 0x79;
    bool ownsLease = false, started = false;
    int result = 1;
    try {
        filter = open_filter();
        hr(CoCreateInstance(__uuidof(MMDeviceEnumerator), nullptr, CLSCTX_ALL, IID_PPV_ARGS(&en)));
        hr(en->EnumAudioEndpoints(eCapture, DEVICE_STATE_ACTIVE, &devices));
        UINT count = 0, matches = 0;
        hr(devices->GetCount(&count));
        for (UINT i = 0; i < count; ++i) {
            IMMDevice* device = nullptr;
            IPropertyStore* properties = nullptr;
            hr(devices->Item(i, &device));
            HRESULT status = device->OpenPropertyStore(STGM_READ, &properties);
            if (FAILED(status)) { device->Release(); hr(status); }
            bool verified = endpoint_is_lab_root(device, properties);
            properties->Release();
            if (verified) {
                ++matches;
                if (!selected) { selected = device; device = nullptr; }
            }
            if (device) device->Release();
        }
        if (matches != 1) throw std::runtime_error("Expected one capture endpoint with verified lab root ancestry");
        LPWSTR endpointId = nullptr;
        hr(selected->GetId(&endpointId));
        fprintf(report, "verified WASAPI endpoint=%ls\n", endpointId);
        CoTaskMemFree(endpointId);
        hr(selected->Activate(__uuidof(IAudioClient), CLSCTX_ALL, nullptr, reinterpret_cast<void**>(&client)));
        hr(client->GetMixFormat(&format));
        SampleKind kind = sample_kind(format);
        hr(client->Initialize(AUDCLNT_SHAREMODE_SHARED, 0, 1000000, 0, format, nullptr));
        hr(client->GetService(IID_PPV_ARGS(&capture)));
        hr(client->Start());
        started = true;
        Stats baseline;
        collect(capture, format, kind, baseline, 200);
        require_silence("before-BEGIN", baseline);

        require_send(filter, message, 1);
        ownsLease = true;
        Message other = message;
        other.nonce[0] ^= 0x01;
        require_denied(filter, other, 1, ERROR_SHARING_VIOLATION);
        require_denied(filter, other, 2, ERROR_ACCESS_DENIED);
        require_denied(filter, other, 3, ERROR_ACCESS_DENIED);
        Stats signal;
        tone(filter, message, capture, format, kind, signal, 100);
        require_tone("signal", signal, 30000);

        // Keep ownership alive but stop PCM: a consumed packet must never replay.
        require_send(filter, message, 4);
        Stats settling;
        collect(capture, format, kind, settling, 300);
        Stats underflow;
        for (unsigned i = 0; i < 2; ++i) {
            require_send(filter, message, 4);
            collect(capture, format, kind, underflow, 100);
        }
        require_silence("underflow-with-live-lease", underflow);
        Stats resumed;
        tone(filter, message, capture, format, kind, resumed, 30);
        require_tone("resumed-after-underflow", resumed, 7000);
        require_send(filter, message, 3);
        ownsLease = false;
        Stats endSettling;
        collect(capture, format, kind, endSettling, 250);
        Stats ended;
        collect(capture, format, kind, ended, 200);
        require_silence("after-END", ended);

        ++message.session;
        ++message.nonce[0];
        require_send(filter, message, 1);
        ownsLease = true;
        require_send(filter, message, 2);
        // No WRITE or KEEPALIVE during this interval; driver lease is two seconds.
        Stats expiryWait;
        collect(capture, format, kind, expiryWait, 2300);
        require_denied(filter, message, 2, ERROR_ACCESS_DENIED);
        ownsLease = false;
        require_denied(filter, message, 4, ERROR_ACCESS_DENIED);
        require_denied(filter, message, 3, ERROR_ACCESS_DENIED);
        Stats expired;
        collect(capture, format, kind, expired, 200);
        require_silence("after-lease-expiry", expired);
        ++message.session;
        ++message.nonce[0];
        require_send(filter, message, 1);
        ownsLease = true;
        require_send(filter, message, 4);
        require_send(filter, message, 3);
        ownsLease = false;
        fprintf(report, "PASS: verified KS-to-WASAPI 900 Hz signal; live-lease underflow/resume; END silence; stale-token rejection and reacquisition after lease expiry.\n");
        result = 0;
    } catch (const std::exception& e) {
        fprintf(report, "FAIL: %s\n", e.what());
    }
    if (filter != INVALID_HANDLE_VALUE) {
        if (ownsLease) {
            KsResult cleanup = send(filter, message, 3);
            fprintf(report, "cleanup END: ok=%u error=%lu\n", cleanup.ok ? 1 : 0, cleanup.error);
            if (!cleanup.ok) result = 1;
        }
        if (!CloseHandle(filter)) { fprintf(report, "CloseHandle error=%lu\n", GetLastError()); result = 1; }
    }
    if (started) {
        HRESULT stopped = client->Stop();
        if (FAILED(stopped)) { fprintf(report, "Stop HRESULT=0x%08lx\n", stopped); result = 1; }
    }
    if (capture) capture->Release();
    if (client) client->Release();
    if (format) CoTaskMemFree(format);
    if (selected) selected->Release();
    if (devices) devices->Release();
    if (en) en->Release();
    CoUninitialize();
    fprintf(report, "FINAL result=%d\n", result);
    fclose(report);
    return result;
}
