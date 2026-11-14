// RemonteVoice host driver helper: creates or removes the root-enumerated
// RemonteVoiceAudio device for a test-signed development driver package.
// Compiled at package build time; invoked only by the elevated setup script.
// No third-party code, no network, no user interface: console output only.
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Text;

namespace RemonteVoice.Setup
{
    internal static class Program
    {
        private const string HardwareId = @"ROOT\RemonteVoiceAudio";
        private const string InfName = "RemonteVoiceAudio.inf";
        private static readonly Guid MediaClass = new Guid("4d36e96c-e325-11ce-bfc1-08002be10318");

        private static int Main(string[] args)
        {
            try
            {
                if (args.Length == 1 && args[0] == "probe") return Probe();
                if (args.Length == 2 && args[0] == "install") return Install(args[1]);
                if (args.Length == 1 && args[0] == "remove") return Remove();
                Console.Error.WriteLine("usage: RemonteVoiceDriverSetup.exe probe | install <inf> | remove");
                return 64;
            }
            catch (Exception error)
            {
                Console.Error.WriteLine("failure: " + error.GetType().Name + ": " + error.Message);
                return 1;
            }
        }

        private static int Probe()
        {
            Console.WriteLine("testsigning=" + (Native.TestSigningActive() ? "on" : "off"));
            IntPtr set = Native.OpenDeviceSet();
            try
            {
                int count = 0;
                string instance = null;
                for (uint index = 0; ; index++)
                {
                    if (index >= 4096) throw new InvalidOperationException("Device enumeration limit exceeded");
                    IntPtr info;
                    if (!Native.EnumDevice(set, index, out info))
                    {
                        if (Marshal.GetLastWin32Error() == 259) break;
                        throw new Win32Exception(Marshal.GetLastWin32Error());
                    }
                    if (!Native.MatchesHardwareId(set, info)) { Native.FreeDevice(info); continue; }
                    count++;
                    instance = Native.InstanceId(set, info);
                    Native.FreeDevice(info);
                }
                if (count > 1) throw new InvalidOperationException("Ambiguous RemonteVoice root devices");
                Console.WriteLine("device=" + (instance ?? "absent"));
                return 0;
            }
            finally { Native.CloseDeviceSet(set); }
        }

        private static int Install(string inf)
        {
            if (!inf.EndsWith(InfName, StringComparison.OrdinalIgnoreCase))
                throw new ArgumentException("Expected " + InfName);
            if (!System.IO.File.Exists(inf)) throw new System.IO.FileNotFoundException(inf);
            if (!Native.TestSigningActive())
                throw new InvalidOperationException("Test signing is not active yet; reboot first");
            Native.InstallRootDevice(IntPtr.Zero, inf, line => Console.WriteLine(line));
            return 0;
        }

        private static int Remove()
        {
            int removed = 0;
            while (true)
            {
                IntPtr set = Native.OpenDeviceSet();
                IntPtr found = IntPtr.Zero;
                try
                {
                    for (uint index = 0; ; index++)
                    {
                        if (index >= 4096) throw new InvalidOperationException("Device enumeration limit exceeded");
                        IntPtr info;
                        if (!Native.EnumDevice(set, index, out info))
                        {
                            if (Marshal.GetLastWin32Error() == 259) break;
                            throw new Win32Exception(Marshal.GetLastWin32Error());
                        }
                        if (!Native.MatchesHardwareId(set, info)) { Native.FreeDevice(info); continue; }
                        found = info; break;
                    }
                    if (found == IntPtr.Zero) break;
                    if (!Native.RemoveDevice(set, found))
                        throw new Win32Exception(Marshal.GetLastWin32Error());
                    removed++;
                    Console.WriteLine("removed=" + Native.InstanceId(set, found));
                }
                finally { Native.CloseDeviceSet(set); }
            }
            Console.WriteLine("devices_removed=" + removed);
            return 0;
        }

        internal static class Native
        {
            [StructLayout(LayoutKind.Sequential)] private struct CodeIntegrity { public uint Length, Options; }
            [DllImport("ntdll.dll")] private static extern int NtQuerySystemInformation(uint kind, ref CodeIntegrity data, uint length, out uint returned);
            public static bool TestSigningActive()
            {
                CodeIntegrity info = new CodeIntegrity(); info.Length = 8; uint returned;
                int status = NtQuerySystemInformation(103, ref info, 8, out returned);
                if (status != 0) throw new InvalidOperationException("Code-integrity query failed: 0x" + status.ToString("X8"));
                return (info.Options & 2) != 0;
            }
            [StructLayout(LayoutKind.Sequential)] private struct DeviceInfo
            { public uint Size; public Guid Class; public uint Instance; public IntPtr Reserved; }
            [StructLayout(LayoutKind.Sequential)] private struct RemoveParameters
            { public uint HeaderSize, Function, Scope, Profile; }
            [DllImport("setupapi.dll", CharSet = CharSet.Unicode, SetLastError = true)] private static extern IntPtr SetupDiGetClassDevsW(ref Guid type, string enumerator, IntPtr owner, uint flags);
            [DllImport("setupapi.dll", SetLastError = true)] private static extern bool SetupDiEnumDeviceInfo(IntPtr set, uint index, ref DeviceInfo info);
            [DllImport("setupapi.dll", SetLastError = true)] private static extern bool SetupDiDestroyDeviceInfoList(IntPtr set);
            [DllImport("setupapi.dll", CharSet = CharSet.Unicode, SetLastError = true)] private static extern bool SetupDiGetDeviceRegistryPropertyW(IntPtr set, ref DeviceInfo info, uint property, out uint kind, byte[] buffer, uint size, out uint required);
            [DllImport("setupapi.dll", CharSet = CharSet.Unicode, SetLastError = true)] private static extern bool SetupDiCreateDeviceInfoW(IntPtr set, string name, ref Guid type, string description, IntPtr owner, uint flags, ref DeviceInfo info);
            [DllImport("setupapi.dll", CharSet = CharSet.Unicode, SetLastError = true)] private static extern bool SetupDiSetDeviceRegistryPropertyW(IntPtr set, ref DeviceInfo info, uint property, byte[] value, uint size);
            [DllImport("setupapi.dll", SetLastError = true)] private static extern bool SetupDiCallClassInstaller(uint function, IntPtr set, ref DeviceInfo info);
            [DllImport("setupapi.dll", CharSet = CharSet.Unicode, SetLastError = true)] private static extern bool SetupDiGetDeviceInstanceIdW(IntPtr set, ref DeviceInfo info, StringBuilder text, uint size, out uint required);
            [DllImport("setupapi.dll", SetLastError = true)] private static extern bool SetupDiSetClassInstallParamsW(IntPtr set, ref DeviceInfo info, ref RemoveParameters parameters, uint size);
            [DllImport("newdev.dll", CharSet = CharSet.Unicode, SetLastError = true)] private static extern bool UpdateDriverForPlugAndPlayDevicesW(IntPtr owner, string hardware, string inf, uint flags, out bool restart);
            [DllImport("cfgmgr32.dll")] public static extern uint CM_Get_DevNode_Status(out uint status, out uint problem, uint instance, uint flags);

            private static DeviceInfo EmptyInfo() { DeviceInfo info = new DeviceInfo(); info.Size = (uint)Marshal.SizeOf(typeof(DeviceInfo)); return info; }
            public static IntPtr OpenDeviceSet()
            {
                Guid type = MediaClass;
                IntPtr set = SetupDiGetClassDevsW(ref type, null, IntPtr.Zero, 2);
                if (set == new IntPtr(-1)) throw new Win32Exception(Marshal.GetLastWin32Error());
                return set;
            }
            public static void CloseDeviceSet(IntPtr set) { if (!SetupDiDestroyDeviceInfoList(set)) { /* best effort */ } }
            public static bool EnumDevice(IntPtr set, uint index, out IntPtr info)
            {
                DeviceInfo value = EmptyInfo();
                bool ok = SetupDiEnumDeviceInfo(set, index, ref value);
                if (!ok) { info = IntPtr.Zero; return false; }
                IntPtr buffer = Marshal.AllocHGlobal((int)value.Size);
                Marshal.StructureToPtr(value, buffer, false);
                info = buffer;
                return true;
            }
            public static bool MatchesHardwareId(IntPtr set, IntPtr handle)
            {
                DeviceInfo info = (DeviceInfo)Marshal.PtrToStructure(handle, typeof(DeviceInfo));
                byte[] hardware = new byte[65536]; uint kind, required;
                if (!SetupDiGetDeviceRegistryPropertyW(set, ref info, 1, out kind, hardware, (uint)hardware.Length, out required))
                {
                    int error = Marshal.GetLastWin32Error();
                    if (error == 13 || error == 1168) return false;
                    throw new Win32Exception(error);
                }
                if (kind != 7 || required < 4 || required > hardware.Length || (required % 2) != 0)
                    throw new InvalidOperationException("Invalid device hardware metadata");
                string ids = Encoding.Unicode.GetString(hardware, 0, (int)required);
                if (!ids.EndsWith("\0\0")) throw new InvalidOperationException("Unterminated hardware identity");
                foreach (string id in ids.Split('\0'))
                    if (String.Equals(id, HardwareId, StringComparison.OrdinalIgnoreCase)) return true;
                return false;
            }
            public static string InstanceId(IntPtr set, IntPtr handle)
            {
                DeviceInfo info = (DeviceInfo)Marshal.PtrToStructure(handle, typeof(DeviceInfo));
                StringBuilder text = new StringBuilder(512); uint needed;
                if (!SetupDiGetDeviceInstanceIdW(set, ref info, text, (uint)text.Capacity, out needed))
                    throw new Win32Exception(Marshal.GetLastWin32Error());
                return text.ToString();
            }
            public static void FreeDevice(IntPtr handle)
            {
                if (handle == IntPtr.Zero) return;
                Marshal.DestroyStructure(handle, typeof(DeviceInfo));
                Marshal.FreeHGlobal(handle);
            }
            private static void Free(IntPtr handle) { FreeDevice(handle); }
            public static bool RemoveDevice(IntPtr set, IntPtr handle)
            {
                DeviceInfo info = (DeviceInfo)Marshal.PtrToStructure(handle, typeof(DeviceInfo));
                RemoveParameters remove = new RemoveParameters();
                remove.HeaderSize = 8; remove.Function = 5; remove.Scope = 1;
                return SetupDiSetClassInstallParamsW(set, ref info, ref remove,
                        (uint)Marshal.SizeOf(typeof(RemoveParameters))) &&
                    SetupDiCallClassInstaller(5, set, ref info);
            }
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
                        IntPtr handle = Marshal.AllocHGlobal((int)info.Size);
                        Marshal.StructureToPtr(info, handle, false);
                        bool match;
                        try { match = MatchesHardwareId(set, handle); }
                        finally { Marshal.DestroyStructure(handle, typeof(DeviceInfo)); Marshal.FreeHGlobal(handle); }
                        if (match) { matches++; selected = info; }
                    }
                    if (matches > 1) throw new InvalidOperationException("Ambiguous RemonteVoice root devices; no installation attempted");
                    if (matches == 0)
                    {
                        if (!SetupDiCreateDeviceInfoW(set, "MEDIA", ref type, "RemonteVoice development audio", owner, 1, ref selected))
                            throw new Win32Exception(Marshal.GetLastWin32Error());
                        byte[] hardware = Encoding.Unicode.GetBytes(HardwareId + "\0\0");
                        if (!SetupDiSetDeviceRegistryPropertyW(set, ref selected, 1, hardware, (uint)hardware.Length))
                            throw new Win32Exception(Marshal.GetLastWin32Error());
                        if (!SetupDiCallClassInstaller(0x19, set, ref selected))
                            throw new Win32Exception(Marshal.GetLastWin32Error());
                        registeredHere = true;
                    }
                    StringBuilder identity = new StringBuilder(512); uint needed;
                    if (!SetupDiGetDeviceInstanceIdW(set, ref selected, identity, (uint)identity.Capacity, out needed))
                        throw new Win32Exception(Marshal.GetLastWin32Error());
                    log("Root device=" + identity + "; existing=" + (matches == 1));
                    bool restart;
                    if (!UpdateDriverForPlugAndPlayDevicesW(owner, HardwareId, inf, 4, out restart))
                        throw new Win32Exception(Marshal.GetLastWin32Error());
                    uint status, problem;
                    uint result = CM_Get_DevNode_Status(out status, out problem, selected.Instance, 0);
                    log("Driver update succeeded; restartRequired=" + restart + "; CMResult=" + result +
                        "; deviceStatus=0x" + status.ToString("X") + "; problem=" + problem);
                    if (result != 0 || problem != 0 || (status & 8) == 0)
                        throw new InvalidOperationException("Device is not confirmed started");
                }
                catch
                {
                    if (registeredHere)
                    {
                        RemoveParameters remove = new RemoveParameters();
                        remove.HeaderSize = 8; remove.Function = 5; remove.Scope = 1;
                        bool removed = SetupDiSetClassInstallParamsW(set, ref selected, ref remove,
                                (uint)Marshal.SizeOf(typeof(RemoveParameters))) &&
                            SetupDiCallClassInstaller(5, set, ref selected);
                        log("New root-device rollback=" + removed);
                    }
                    throw;
                }
                finally { CloseDeviceSet(set); }
            }
        }
    }
}
