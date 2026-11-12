// Private guest probe. Opens only the exact RemonteVoice lab KS filter.
// cl /nologo /std:c++17 /EHsc /W3 /WX /MT acl_probe.cpp /link setupapi.lib advapi32.lib
#define NOMINMAX
#include <windows.h>
#include <setupapi.h>
#include <cfgmgr32.h>
#include <ks.h>
#include <algorithm>
#include <cstdarg>
#include <cstdio>
#include <cstring>
#include <cwchar>
#include <stdexcept>
#include <string>
#include <vector>

namespace {
constexpr wchar_t kRoot[] = L"ROOT\\MEDIA\\0000";
constexpr wchar_t kHardware[] = L"ROOT\\RemonteVoiceAudio";
constexpr ULONG kBegin = 1, kWrite = 2, kEnd = 3, kKeepalive = 4;

class Report {
    FILE* file_ = nullptr;
    size_t remaining_ = 32768;
public:
    Report() {
        const wchar_t* directory = L"C:\\Windows\\Temp\\RemonteVoiceLab-20261025";
        if (!CreateDirectoryW(directory, nullptr) && GetLastError() != ERROR_ALREADY_EXISTS) return;
        const DWORD attributes = GetFileAttributesW(directory);
        if (attributes == INVALID_FILE_ATTRIBUTES || !(attributes & FILE_ATTRIBUTE_DIRECTORY)) return;
        _wfopen_s(&file_, L"C:\\Windows\\Temp\\RemonteVoiceLab-20261025\\acl-probe.txt", L"w");
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
        const size_t count = std::min(remaining_, strlen(line));
        fwrite(line, 1, count, file_);
        remaining_ -= count;
        fflush(file_);
    }
};

void failure(Report& report, const char* operation, DWORD error) {
    report.log("%s error=%lu\n", operation, error);
    throw std::runtime_error(operation);
}

struct Message {
    ULONG version, command;
    ULONGLONG session;
    BYTE nonce[16];
    ULONG size, reserved;
    short pcm[480];
};
static_assert(sizeof(Message) == 1000, "PCM ingress wire ABI");

struct KsResult { bool ok; DWORD error; };
KsResult send(HANDLE handle, Message& message, ULONG command) {
    KSPROPERTY property = {};
    property.Set = {0xee47bfaa, 0x9650, 0x41d5, {0xaf, 0xa0, 0x09, 0xce, 0x9f, 0x32, 0xe7, 0x1b}};
    property.Id = 0;
    property.Flags = KSPROPERTY_TYPE_SET;
    message.command = command;
    message.size = command == kWrite ? static_cast<ULONG>(sizeof(message.pcm)) : 0;
    DWORD returned = 0;
    const BOOL ok = DeviceIoControl(handle, 0x002f0003, &property, sizeof(property),
        &message, sizeof(message), &returned, nullptr);
    return {ok != FALSE, ok ? ERROR_SUCCESS : GetLastError()};
}

bool valid_hardware_ids(const wchar_t* ids, DWORD bytes) {
    if (bytes < 2 * sizeof(wchar_t) || bytes % sizeof(wchar_t)) return false;
    const size_t count = bytes / sizeof(wchar_t);
    if (ids[count - 1] || ids[count - 2]) return false;
    for (size_t offset = 0; offset < count - 1 && ids[offset];) {
        const size_t length = wcsnlen_s(ids + offset, count - offset);
        if (length == count - offset) return false;
        if (_wcsicmp(ids + offset, kHardware) == 0) return true;
        offset += length + 1;
    }
    return false;
}

std::wstring filter_path(Report& report) {
    const GUID category = {0x6994ad04, 0x93ef, 0x11d0, {0xa3, 0xcc, 0, 0xa0, 0xc9, 0x22, 0x31, 0x96}};
    struct DeviceSet {
        HDEVINFO handle;
        ~DeviceSet() { if (handle != INVALID_HANDLE_VALUE) SetupDiDestroyDeviceInfoList(handle); }
    } set{SetupDiGetClassDevsW(&category, nullptr, nullptr, DIGCF_PRESENT | DIGCF_DEVICEINTERFACE)};
    if (set.handle == INVALID_HANDLE_VALUE) failure(report, "SetupDiGetClassDevs", GetLastError());
    std::wstring selected;
    unsigned matches = 0;
    for (DWORD index = 0;; ++index) {
        if (index >= 4096) throw std::runtime_error("Interface enumeration exceeded bound");
        SP_DEVICE_INTERFACE_DATA face = {};
        face.cbSize = sizeof(face);
        if (!SetupDiEnumDeviceInterfaces(set.handle, nullptr, &category, index, &face)) {
            const DWORD error = GetLastError();
            if (error != ERROR_NO_MORE_ITEMS) failure(report, "SetupDiEnumDeviceInterfaces", error);
            break;
        }
        DWORD bytes = 0;
        const BOOL sized = SetupDiGetDeviceInterfaceDetailW(set.handle, &face, nullptr, 0, &bytes, nullptr);
        if (sized || GetLastError() != ERROR_INSUFFICIENT_BUFFER || bytes < sizeof(SP_DEVICE_INTERFACE_DETAIL_DATA_W) || bytes > 65536)
            throw std::runtime_error("Invalid interface detail size/result");
        std::vector<BYTE> data(bytes);
        auto* detail = reinterpret_cast<SP_DEVICE_INTERFACE_DETAIL_DATA_W*>(data.data());
        detail->cbSize = sizeof(*detail);
        SP_DEVINFO_DATA device = {};
        device.cbSize = sizeof(device);
        if (!SetupDiGetDeviceInterfaceDetailW(set.handle, &face, detail, bytes, nullptr, &device))
            failure(report, "SetupDiGetDeviceInterfaceDetail", GetLastError());
        const std::wstring path = detail->DevicePath;
        constexpr wchar_t suffix[] = L"\\wavemicarray1";
        constexpr size_t suffixLength = sizeof(suffix) / sizeof(wchar_t) - 1;
        if (path.size() < suffixLength || _wcsicmp(path.c_str() + path.size() - suffixLength, suffix)) continue;
        wchar_t instance[MAX_DEVICE_ID_LEN] = {};
        if (!SetupDiGetDeviceInstanceIdW(set.handle, &device, instance, MAX_DEVICE_ID_LEN, nullptr))
            failure(report, "SetupDiGetDeviceInstanceId", GetLastError());
        if (_wcsicmp(instance, kRoot)) continue;
        wchar_t ids[4096] = {};
        DWORD type = 0, required = 0;
        if (!SetupDiGetDeviceRegistryPropertyW(set.handle, &device, SPDRP_HARDWAREID, &type,
            reinterpret_cast<BYTE*>(ids), sizeof(ids), &required))
            failure(report, "SetupDiGetDeviceRegistryProperty", GetLastError());
        if (type != REG_MULTI_SZ || required > sizeof(ids) || !valid_hardware_ids(ids, required))
            throw std::runtime_error("Lab root hardware identity mismatch");
        selected = path;
        ++matches;
    }
    report.log("verifiedFilterMatches=%u root=%ls hardware=%ls\n", matches, kRoot, kHardware);
    if (matches != 1) throw std::runtime_error("Expected exactly one verified lab mic filter");
    report.log("verifiedKSFilter=%ls\n", selected.c_str());
    return selected;
}

struct Context {
    Report& report;
    HANDLE processToken = nullptr, restrictedToken = nullptr;
    HANDLE adminFilter = INVALID_HANDLE_VALUE, restrictedFilter = INVALID_HANDLE_VALUE;
    Message message = {};
    bool impersonating = false, lease = false;
    explicit Context(Report& value) : report(value) {}
    ~Context() { cleanup(); }
    bool cleanup() {
        bool ok = true;
        if (impersonating) {
            if (RevertToSelf()) impersonating = false;
            else {
                report.log("cleanup RevertToSelf error=%lu\n", GetLastError());
                // Same documented operation, using SetThreadToken directly as a fallback.
                if (SetThreadToken(nullptr, nullptr)) impersonating = false;
                else { report.log("cleanup SetThreadToken(NULL) error=%lu\n", GetLastError()); ok = false; }
            }
        }
        if (lease) {
            if (!impersonating) {
                const KsResult ended = send(adminFilter, message, kEnd);
                report.log("cleanup END ok=%u error=%lu\n", ended.ok ? 1u : 0u, ended.error);
                if (!ended.ok) ok = false;
            } else {
                report.log("cleanup could not restore token; lease must expire; FAIL\n");
                ok = false;
            }
            lease = false;
        }
        HANDLE* handles[] = {&restrictedFilter, &adminFilter, &restrictedToken, &processToken};
        for (HANDLE* handle : handles) {
            if (*handle && *handle != INVALID_HANDLE_VALUE) {
                if (!CloseHandle(*handle)) { report.log("cleanup CloseHandle error=%lu\n", GetLastError()); ok = false; }
                *handle = nullptr;
            }
        }
        SecureZeroMemory(&message, sizeof(message));
        return ok;
    }
};

void require_member(Report& report, PSID adminSid, bool expected, const char* label) {
    BOOL member = FALSE;
    if (!CheckTokenMembership(nullptr, adminSid, &member)) failure(report, "CheckTokenMembership", GetLastError());
    report.log("%s administratorEnabled=%u expected=%u\n", label, member ? 1u : 0u, expected ? 1u : 0u);
    if ((member != FALSE) != expected) throw std::runtime_error("Unexpected effective administrator membership");
}

void require_success(Context& context, ULONG command, const char* label) {
    const KsResult result = send(context.adminFilter, context.message, command);
    context.report.log("%s command=%lu ok=%u error=%lu\n", label, command, result.ok ? 1u : 0u, result.error);
    if (!result.ok) throw std::runtime_error("Privileged KS command failed");
    if (command == kBegin) context.lease = true;
    if (command == kEnd) context.lease = false;
}

void require_denied(Context& context, HANDLE handle, const char* label) {
    const ULONG commands[] = {kBegin, kWrite, kKeepalive, kEnd};
    for (ULONG command : commands) {
        const KsResult result = send(handle, context.message, command);
        context.report.log("%s command=%lu ok=%u error=%lu expected=%lu\n", label, command,
            result.ok ? 1u : 0u, result.error, static_cast<DWORD>(ERROR_ACCESS_DENIED));
        if (result.ok || result.error != ERROR_ACCESS_DENIED)
            throw std::runtime_error("Restricted KS command did not return ERROR_ACCESS_DENIED");
    }
}

void run(Context& context) {
    Report& report = context.report;
    // Do not replace an unknown pre-existing impersonation context.
    HANDLE existing = nullptr;
    if (OpenThreadToken(GetCurrentThread(), TOKEN_QUERY, TRUE, &existing)) {
        CloseHandle(existing);
        throw std::runtime_error("Probe must start without a thread impersonation token");
    }
    if (GetLastError() != ERROR_NO_TOKEN) failure(report, "Initial OpenThreadToken", GetLastError());
    alignas(DWORD) BYTE adminSid[SECURITY_MAX_SID_SIZE] = {};
    DWORD sidBytes = sizeof(adminSid);
    if (!CreateWellKnownSid(WinBuiltinAdministratorsSid, nullptr, adminSid, &sidBytes))
        failure(report, "CreateWellKnownSid(Administrators)", GetLastError());
    require_member(report, adminSid, true, "baseline");
    if (!OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY | TOKEN_DUPLICATE | TOKEN_IMPERSONATE, &context.processToken))
        failure(report, "OpenProcessToken", GetLastError());
    DWORD userBytes = 0;
    GetTokenInformation(context.processToken, TokenUser, nullptr, 0, &userBytes);
    if (GetLastError() != ERROR_INSUFFICIENT_BUFFER || userBytes < sizeof(TOKEN_USER) || userBytes > 4096)
        throw std::runtime_error("Invalid TokenUser size");
    std::vector<BYTE> userData(userBytes);
    if (!GetTokenInformation(context.processToken, TokenUser, userData.data(), userBytes, &userBytes))
        failure(report, "GetTokenInformation(TokenUser)", GetLastError());
    if (IsWellKnownSid(reinterpret_cast<TOKEN_USER*>(userData.data())->User.Sid, WinLocalSystemSid))
        throw std::runtime_error("Run as an elevated ordinary administrator, not LocalSystem");
    SID_AND_ATTRIBUTES disabled = {adminSid, 0};
    if (!CreateRestrictedToken(context.processToken, DISABLE_MAX_PRIVILEGE, 1, &disabled,
        0, nullptr, 0, nullptr, &context.restrictedToken))
        failure(report, "CreateRestrictedToken", GetLastError());
    const std::wstring path = filter_path(report);
    context.adminFilter = CreateFileW(path.c_str(), GENERIC_READ | GENERIC_WRITE,
        FILE_SHARE_READ | FILE_SHARE_WRITE, nullptr, OPEN_EXISTING, 0, nullptr);
    if (context.adminFilter == INVALID_HANDLE_VALUE) failure(report, "Admin CreateFile", GetLastError());
    context.message.version = 1;
    context.message.session = (GetTickCount64() << 16) ^ GetCurrentProcessId() ^ 0x525641434cULL;
    if (!context.message.session) context.message.session = 1;
    for (unsigned i = 0; i < sizeof(context.message.nonce); ++i)
        context.message.nonce[i] = static_cast<BYTE>(0x51 + i);
    require_success(context, kBegin, "baseline");
    require_success(context, kKeepalive, "baseline");
    require_success(context, kEnd, "baseline");
    require_success(context, kBegin, "held lease");
    require_success(context, kKeepalive, "held lease");
    const ULONGLONG restrictedStarted = GetTickCount64();
    if (!ImpersonateLoggedOnUser(context.restrictedToken)) failure(report, "ImpersonateLoggedOnUser", GetLastError());
    context.impersonating = true;
    require_member(report, adminSid, false, "restricted impersonation");
    context.restrictedFilter = CreateFileW(path.c_str(), GENERIC_READ | GENERIC_WRITE,
        FILE_SHARE_READ | FILE_SHARE_WRITE, nullptr, OPEN_EXISTING, 0, nullptr);
    if (context.restrictedFilter == INVALID_HANDLE_VALUE)
        failure(report, "Restricted CreateFile must permit standard filter access", GetLastError());
    report.log("restricted CreateFile succeeded; same PID/session/nonce retained for all requests\n");
    require_denied(context, context.restrictedFilter, "restricted-open handle");
    require_denied(context, context.adminFilter, "borrowed privileged handle");
    if (!RevertToSelf()) failure(report, "RevertToSelf", GetLastError());
    context.impersonating = false;
    const ULONGLONG restrictedElapsed = GetTickCount64() - restrictedStarted;
    report.log("restrictedWindowMs=%llu maximum=1500 leaseMs=2000\n", restrictedElapsed);
    require_member(report, adminSid, true, "restored administrator");
    // Success proves the original lease survived all restricted requests and did
    // not expire or get ended; bridge ownership alone cannot explain the denials.
    require_success(context, kKeepalive, "after revert original lease");
    require_success(context, kEnd, "after revert original lease");
    if (restrictedElapsed > 1500) throw std::runtime_error("Restricted test exceeded safe lease timing window");
}
} // namespace

int main() {
    Report report;
    if (!report) return 1;
    report.log("RemonteVoice private KS per-request ACL probe; report capped at 32768 bytes.\n");
    Context context(report);
    bool passed = false;
    try { run(context); passed = true; }
    catch (const std::exception& error) { report.log("FAIL: %s\n", error.what()); }
    catch (...) { report.log("FAIL: unexpected exception\n"); }
    if (!context.cleanup()) passed = false;
    if (passed) report.log("PASS: admin commands succeed; restricted filter opens; all four commands denied on both new and borrowed handles; original admin lease retained and ended; tokens/handles closed.\n");
    return passed ? 0 : 1;
}
