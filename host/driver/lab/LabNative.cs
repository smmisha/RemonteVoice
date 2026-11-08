using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Text;

namespace RemoteVoice.DriverLab
{
    static class Native
    {
        const string HardwareId = @"ROOT\RemoteVoiceAudio";
        static readonly Guid MediaClass = new Guid("4d36e96c-e325-11ce-bfc1-08002be10318");
        [StructLayout(LayoutKind.Sequential)] struct CodeIntegrity { public uint Length, Options; }
        [DllImport("ntdll.dll")] static extern int NtQuerySystemInformation(uint kind, ref CodeIntegrity data, uint length, out uint returned);
        public static bool TestSigningActive()
        {
            CodeIntegrity info = new CodeIntegrity(); info.Length = 8; uint returned;
            int status = NtQuerySystemInformation(103, ref info, 8, out returned);
            if (status != 0) throw new InvalidOperationException("Code-integrity query failed: 0x" + status.ToString("X8"));
            return (info.Options & 2) != 0;
        }
        [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)] struct TrustFile
        { public uint Size; [MarshalAs(UnmanagedType.LPWStr)] public string File; public IntPtr Handle, Subject; }
        [StructLayout(LayoutKind.Sequential)] struct TrustData
        {
            public uint Size; public IntPtr Policy, Sip; public uint Ui, Revocation, Choice;
            public IntPtr File; public uint Action; public IntPtr State, Url; public uint Flags, Context; public IntPtr Signature;
        }
        [DllImport("wintrust.dll", ExactSpelling = true)] static extern int WinVerifyTrust(IntPtr owner, ref Guid action, ref TrustData data);
        public static void VerifySignature(string path)
        {
            TrustFile file = new TrustFile(); file.Size = (uint)Marshal.SizeOf(typeof(TrustFile)); file.File = path;
            IntPtr buffer = Marshal.AllocHGlobal((int)file.Size);
            Marshal.StructureToPtr(file, buffer, false);
            TrustData data = new TrustData(); data.Size = (uint)Marshal.SizeOf(typeof(TrustData));
            data.Ui = 2; data.Choice = 1; data.File = buffer; data.Action = 1;
            data.Flags = 0x1000 | 0x10; // Offline/cache-only verification; no online revocation request.
            Guid action = new Guid("00aac56b-cd44-11d0-8cc2-00c04fc295ee");
            try
            {
                int status = WinVerifyTrust(IntPtr.Zero, ref action, ref data);
                if (status != 0) throw new InvalidOperationException("Guest signature verification failed: 0x" + status.ToString("X8"));
            }
            finally
            {
                data.Action = 2; WinVerifyTrust(IntPtr.Zero, ref action, ref data);
                Marshal.DestroyStructure(buffer, typeof(TrustFile)); Marshal.FreeHGlobal(buffer);
            }
        }
        [StructLayout(LayoutKind.Sequential)] struct DeviceInfo
        { public uint Size; public Guid Class; public uint Instance; public IntPtr Reserved; }
        [StructLayout(LayoutKind.Sequential)] struct RemoveParameters
        { public uint HeaderSize, Function, Scope, Profile; }
        [DllImport("setupapi.dll", CharSet = CharSet.Unicode, SetLastError = true)] static extern IntPtr SetupDiGetClassDevsW(ref Guid type, string enumerator, IntPtr owner, uint flags);
        [DllImport("setupapi.dll", SetLastError = true)] static extern bool SetupDiEnumDeviceInfo(IntPtr set, uint index, ref DeviceInfo info);
        [DllImport("setupapi.dll", CharSet = CharSet.Unicode, SetLastError = true)] static extern bool SetupDiGetDeviceRegistryPropertyW(IntPtr set, ref DeviceInfo info, uint property, out uint kind, byte[] buffer, uint size, out uint required);
        [DllImport("setupapi.dll", CharSet = CharSet.Unicode, SetLastError = true)] static extern bool SetupDiCreateDeviceInfoW(IntPtr set, string name, ref Guid type, string description, IntPtr owner, uint flags, ref DeviceInfo info);
        [DllImport("setupapi.dll", CharSet = CharSet.Unicode, SetLastError = true)] static extern bool SetupDiSetDeviceRegistryPropertyW(IntPtr set, ref DeviceInfo info, uint property, byte[] value, uint size);
        [DllImport("setupapi.dll", SetLastError = true)] static extern bool SetupDiCallClassInstaller(uint function, IntPtr set, ref DeviceInfo info);
        [DllImport("setupapi.dll", CharSet = CharSet.Unicode, SetLastError = true)] static extern bool SetupDiGetDeviceInstanceIdW(IntPtr set, ref DeviceInfo info, StringBuilder text, uint size, out uint required);
        [DllImport("setupapi.dll", SetLastError = true)] static extern bool SetupDiSetClassInstallParamsW(IntPtr set, ref DeviceInfo info, ref RemoveParameters parameters, uint size);
        [DllImport("setupapi.dll", SetLastError = true)] static extern bool SetupDiDestroyDeviceInfoList(IntPtr set);
        [DllImport("newdev.dll", CharSet = CharSet.Unicode, SetLastError = true)] static extern bool UpdateDriverForPlugAndPlayDevicesW(IntPtr owner, string hardware, string inf, uint flags, out bool restart);
        [DllImport("cfgmgr32.dll")] static extern uint CM_Get_DevNode_Status(out uint status, out uint problem, uint instance, uint flags);
        static void Check(bool success) { if (!success) throw new Win32Exception(Marshal.GetLastWin32Error()); }
        static DeviceInfo EmptyInfo() { DeviceInfo info = new DeviceInfo(); info.Size = (uint)Marshal.SizeOf(typeof(DeviceInfo)); return info; }
        public static void InstallRootDevice(IntPtr owner, string inf, Action<string> log)
        {
            Guid type = MediaClass;
            IntPtr set = SetupDiGetClassDevsW(ref type, null, owner, 2);
            if (set == new IntPtr(-1)) throw new Win32Exception(Marshal.GetLastWin32Error());
            DeviceInfo selected = EmptyInfo(); uint matches = 0; bool registeredHere = false;
            try
            {
                for (uint index = 0; ; index++)
                {
                    if (index >= 4096) throw new InvalidOperationException("Device enumeration limit exceeded");
                    DeviceInfo info = EmptyInfo();
                    if (!SetupDiEnumDeviceInfo(set, index, ref info))
                    {
                        if (Marshal.GetLastWin32Error() == 259) break;
                        throw new Win32Exception(Marshal.GetLastWin32Error());
                    }
                    byte[] hardware = new byte[65536]; uint kind, required;
                    if (!SetupDiGetDeviceRegistryPropertyW(set, ref info, 1, out kind, hardware, (uint)hardware.Length, out required))
                    {
                        int error = Marshal.GetLastWin32Error();
                        if (error == 13 || error == 1168) continue;
                        throw new Win32Exception(error);
                    }
                    if (kind != 7 || required < 4 || required > hardware.Length || (required % 2) != 0)
                        throw new InvalidOperationException("Invalid device hardware metadata");
                    string ids = Encoding.Unicode.GetString(hardware, 0, (int)required);
                    if (!ids.EndsWith("\0\0")) throw new InvalidOperationException("Unterminated hardware identity");
                    foreach (string id in ids.Split('\0'))
                        if (String.Equals(id, HardwareId, StringComparison.OrdinalIgnoreCase)) { matches++; selected = info; break; }
                }
                if (matches > 1) throw new InvalidOperationException("Ambiguous RemoteVoice root devices; no installation attempted");
                if (matches == 0)
                {
                    Check(SetupDiCreateDeviceInfoW(set, "MEDIA", ref type, "RemoteVoice development audio", owner, 1, ref selected));
                    byte[] hardware = Encoding.Unicode.GetBytes(HardwareId + "\0\0");
                    Check(SetupDiSetDeviceRegistryPropertyW(set, ref selected, 1, hardware, (uint)hardware.Length));
                    Check(SetupDiCallClassInstaller(0x19, set, ref selected)); registeredHere = true;
                }
                StringBuilder identity = new StringBuilder(512); uint needed;
                Check(SetupDiGetDeviceInstanceIdW(set, ref selected, identity, (uint)identity.Capacity, out needed));
                log("Root device=" + identity + "; existing=" + (matches == 1));
                bool restart;
                Check(UpdateDriverForPlugAndPlayDevicesW(owner, HardwareId, inf, 4, out restart));
                uint status, problem; uint result = CM_Get_DevNode_Status(out status, out problem, selected.Instance, 0);
                log("Driver update returned success; restartRequired=" + restart + "; CMResult=" + result + "; deviceStatus=0x" + status.ToString("X") + "; problem=" + problem);
                if (result != 0 || problem != 0 || (status & 8) == 0)
                    throw new InvalidOperationException("Device is not confirmed started; inspect guest logs/restart status");
                log("Device started. Actual PCM and WASAPI acceptance remain to be run.");
            }
            catch
            {
                if (registeredHere)
                {
                    RemoveParameters remove = new RemoveParameters(); remove.HeaderSize = 8; remove.Function = 5; remove.Scope = 1;
                    bool removed = SetupDiSetClassInstallParamsW(set, ref selected, ref remove, (uint)Marshal.SizeOf(typeof(RemoveParameters))) && SetupDiCallClassInstaller(5, set, ref selected);
                    log("New root-device rollback=" + removed + "; imported package may remain; snapshot recovery is still required.");
                }
                throw;
            }
            finally { if (!SetupDiDestroyDeviceInfoList(set)) log("Device-info cleanup unconfirmed"); }
        }
        [StructLayout(LayoutKind.Sequential, Pack = 4)] struct Privileges { public uint Count; public long Luid; public uint Attributes; }
        [DllImport("advapi32.dll", SetLastError = true)] static extern bool OpenProcessToken(IntPtr process, uint access, out IntPtr token);
        [DllImport("advapi32.dll", CharSet = CharSet.Unicode, SetLastError = true)] static extern bool LookupPrivilegeValue(string system, string name, out long luid);
        [DllImport("advapi32.dll", SetLastError = true)] static extern bool AdjustTokenPrivileges(IntPtr token, bool disable, ref Privileges state, uint length, IntPtr previous, IntPtr returned);
        [DllImport("kernel32.dll")] static extern IntPtr GetCurrentProcess();
        [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr handle);
        [DllImport("user32.dll", SetLastError = true)] static extern bool ExitWindowsEx(uint flags, uint reason);
        public static void Reboot()
        {
            IntPtr token; Check(OpenProcessToken(GetCurrentProcess(), 0x28, out token));
            try
            {
                Privileges privileges = new Privileges(); privileges.Count = 1; privileges.Attributes = 2;
                Check(LookupPrivilegeValue(null, "SeShutdownPrivilege", out privileges.Luid));
                Check(AdjustTokenPrivileges(token, false, ref privileges, 0, IntPtr.Zero, IntPtr.Zero));
                if (Marshal.GetLastWin32Error() != 0) throw new Win32Exception(Marshal.GetLastWin32Error());
                Check(ExitWindowsEx(2, 0x80030002)); // Ordinary guest restart; no forced application termination.
            }
            finally { CloseHandle(token); }
        }
    }
}
