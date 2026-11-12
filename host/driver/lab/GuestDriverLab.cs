// Private development-VM helper. Not a production installer or authorization boundary.
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Management;
using System.Security.Cryptography;
using System.Security.Cryptography.X509Certificates;
using System.Security.Principal;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using System.Windows.Forms;

namespace RemonteVoice.DriverLab
{
    static class Program
    {
        [STAThread]
        static int Main(string[] args)
        {
            string directory = AppDomain.CurrentDomain.BaseDirectory;
            if (args.Length == 1 && args[0] == "--probe")
            {
                // Read-only negative-guard test on the host; no elevation or BCD/store access.
                bool accepted = false; string result;
                try { new LabOps(directory, null).RequireTarget(); accepted = true; result = "target accepted"; }
                catch (Exception error) { result = error.Message; }
                File.WriteAllText(Path.Combine(directory, "host-guard-probe.json"),
                    new JavaScriptSerializer().Serialize(new { accepted = accepted, result = result }));
                return accepted ? 0 : 3;
            }
            if (args.Length == 1 && args[0].StartsWith("--headless-", StringComparison.Ordinal))
            {
                string action = args[0].Substring("--headless-".Length);
                if (action != "inspect" && action != "setup" && action != "install") return 2;
                bool success = false; string errorText = "";
                LabOps ops = null;
                try
                {
                    ops = new LabOps(directory, null, true);
                    ops.RequireTarget(); ops.VerifyPackage();
                    if (action == "inspect") ops.Inspect();
                    else if (action == "setup") ops.PrepareTestMode();
                    else ops.Install(IntPtr.Zero);
                    success = true;
                }
                catch (Exception error)
                {
                    errorText = error.Message;
                    if (ops != null) { try { ops.Log("FAILED: " + errorText); } catch { } }
                }
                try
                {
                    File.WriteAllText(Path.Combine(directory, "headless-" + action + ".json"),
                        new JavaScriptSerializer().Serialize(new { success = success, action = action,
                            error = errorText, finished_utc = DateTime.UtcNow.ToString("o") }));
                }
                catch { return 4; } // Report unavailable: caller must inspect, never assume success.
                return success ? 0 : 1;
            }
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            Application.Run(new LabForm(directory, args.Length == 1 ? args[0] : ""));
            return 0;
        }
    }

    sealed class LabForm : Form
    {
        readonly TextBox output = new TextBox();
        readonly FlowLayoutPanel actions = new FlowLayoutPanel();
        readonly LabOps ops;
        readonly string initialAction;
        public LabForm(string directory, string action)
        {
            Text = "RemonteVoice — development VM only"; Width = 940; Height = 620;
            initialAction = action;
            output.Multiline = true; output.ReadOnly = true; output.ScrollBars = ScrollBars.Both;
            output.Dock = DockStyle.Fill; output.Font = new Font("Consolas", 10);
            actions.Dock = DockStyle.Top; actions.Height = 74;
            Controls.Add(output); Controls.Add(actions);
            ops = new LabOps(directory, Write);
            AddButton("1. Проверить VM", "inspect");
            AddButton("2. Тестовая подпись в VM", "setup");
            AddButton("3. Перезагрузить VM", "reboot");
            AddButton("4. Установить драйвер", "install");
            Shown += delegate { Execute(initialAction == "" ? "inspect" : initialAction); };
        }
        void AddButton(string title, string action)
        {
            Button button = new Button(); button.Text = title; button.AutoSize = true;
            button.Click += delegate { Execute(action); }; actions.Controls.Add(button);
        }
        void Write(string value)
        {
            if (InvokeRequired) { BeginInvoke((Action<string>)Write, value); return; }
            output.AppendText(value + Environment.NewLine);
        }
        void Execute(string action)
        {
            actions.Enabled = false;
            IntPtr owner = Handle;
            Task.Factory.StartNew(delegate {
                try
                {
                    ops.RequireTarget(); ops.VerifyPackage();
                    if (action != "inspect" && !LabOps.IsAdministrator())
                    {
                        // Target guard runs BEFORE the ordinary guest UAC request.
                        Process.Start(new ProcessStartInfo(Application.ExecutablePath, action) {
                            UseShellExecute = true, Verb = "runas", WorkingDirectory = ops.DirectoryPath });
                        ops.Log("Elevated helper requested in the verified guest; confirm its UAC manually.");
                        return;
                    }
                    if (action == "inspect") ops.Inspect();
                    else if (action == "setup") ops.PrepareTestMode();
                    else if (action == "install") ops.Install(owner);
                    else if (action == "reboot")
                    {
                        if (MessageBox.Show("Перезагрузить только эту тестовую Windows?", "RemonteVoice VM",
                            MessageBoxButtons.YesNo) == DialogResult.Yes) ops.Reboot();
                    }
                    else throw new InvalidOperationException("Unknown lab action");
                }
                catch (Exception error) { ops.Log("FAILED: " + error.Message); }
                finally { if (!IsDisposed) BeginInvoke((Action)delegate { actions.Enabled = true; }); }
            });
        }
    }

    sealed class LabOps
    {
        public readonly string DirectoryPath;
        readonly Dictionary<string, object> config;
        readonly Action<string> display;
        readonly bool headless;
        bool targetVerified;
        const uint TestSigningElement = 0x16000049;
        public LabOps(string directory, Action<string> log, bool headlessMode = false)
        {
            DirectoryPath = directory; display = log; headless = headlessMode;
            config = new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(
                File.ReadAllText(Path.Combine(directory, "lab-config.json")));
        }
        static string Query(string type, string property)
        {
            using (ManagementObjectSearcher query = new ManagementObjectSearcher(
                "SELECT " + property + " FROM " + type))
            using (ManagementObjectCollection values = query.Get())
            {
                foreach (ManagementObject value in values)
                    using (value) { return Convert.ToString(value[property]); }
            }
            throw new InvalidOperationException("Guest identity unavailable");
        }
        static string Normalize(string value)
        {
            value = Regex.Replace(value, "^VMware-", "", RegexOptions.IgnoreCase);
            return Regex.Replace(value, "[^0-9a-fA-F]", "").ToLowerInvariant();
        }
        public void RequireTarget()
        {
            if (!Environment.Is64BitProcess || !Environment.Is64BitOperatingSystem)
                throw new InvalidOperationException("Windows x64 required");
            if (!Query("Win32_ComputerSystem", "Manufacturer").StartsWith("VMware", StringComparison.OrdinalIgnoreCase))
                throw new InvalidOperationException("VMware guest only — refusing host operation");
            string actual = Normalize(Query("Win32_BIOS", "SerialNumber"));
            string expected = Normalize(Convert.ToString(config["vm_bios_uuid"]));
            if (actual.Length != 32 || expected.Length != 32 || actual != expected)
                throw new InvalidOperationException("Wrong VMware guest — expected BIOS identity does not match");
            if (Query("Win32_OperatingSystem", "BuildNumber") != Convert.ToString(config["guest_build"]))
                throw new InvalidOperationException("Unexpected guest Windows build");
            targetVerified = true;
        }
        public static bool IsAdministrator()
        {
            using (WindowsIdentity identity = WindowsIdentity.GetCurrent())
                return new WindowsPrincipal(identity).IsInRole(WindowsBuiltInRole.Administrator);
        }
        void RequireAdministrator()
        {
            RequireTarget();
            if (!IsAdministrator()) throw new InvalidOperationException("Guest administrator rights required");
        }
        public void VerifyPackage()
        {
            Dictionary<string, object> files = (Dictionary<string, object>)config["files"];
            string[] required = { "RemonteVoiceAudio.sys", "RemonteVoiceAudio.inf", "RemonteVoiceAudio.cat", "RemonteVoiceLab.cer", "GuestDriverLab.exe" };
            if (files.Count != required.Length) throw new InvalidOperationException("Unexpected package file set");
            foreach (string name in required)
                if (!files.ContainsKey(name)) throw new InvalidOperationException("Required package hash missing: " + name);
            foreach (KeyValuePair<string, object> item in files)
            {
                if (Path.GetFileName(item.Key) != item.Key) throw new InvalidOperationException("Invalid package filename");
                using (SHA256 hash = SHA256.Create())
                using (FileStream stream = File.OpenRead(Path.Combine(DirectoryPath, item.Key)))
                {
                    string digest = BitConverter.ToString(hash.ComputeHash(stream)).Replace("-", "").ToLowerInvariant();
                    if (digest != Convert.ToString(item.Value)) throw new InvalidOperationException("Package hash mismatch: " + item.Key);
                }
            }
            using (X509Certificate2 certificate = Certificate())
                if (!String.Equals(certificate.Thumbprint, Convert.ToString(config["certificate_thumbprint"]), StringComparison.OrdinalIgnoreCase))
                    throw new InvalidOperationException("Unexpected public test certificate");
        }
        X509Certificate2 Certificate() { return new X509Certificate2(Path.Combine(DirectoryPath, "RemonteVoiceLab.cer")); }
        public void Log(string text)
        {
            string line = DateTime.UtcNow.ToString("o") + " " + text;
            if (display != null) display(line);
            if (!targetVerified) return; // Never access VMware shares from the host guard probe.
            string reports = headless ? Path.Combine(DirectoryPath, "reports") : @"\\vmware-host\Shared Folders\RemonteVoiceLabResults";
            if (!headless && !System.IO.Directory.Exists(reports)) reports = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "RemonteVoiceDriverLab");
            System.IO.Directory.CreateDirectory(reports);
            string path = Path.Combine(reports, "lab-report.txt");
            string previous = File.Exists(path) ? File.ReadAllText(path) : "";
            if (previous.Length > 131072) previous = previous.Substring(previous.Length - 131072);
            File.WriteAllText(path, previous + line + Environment.NewLine);
        }
        public void Inspect()
        {
            RequireTarget(); VerifyPackage();
            Log("Verified expected VMware identity, Windows build " + config["guest_build"] + ", x64 and package hashes.");
            Log("Administrator=" + IsAdministrator() + "; current kernel test-signing=" + Native.TestSigningActive());
            try { bool? mode = ReadTestMode(); Log("BCD test-signing=" + (mode.HasValue ? mode.ToString() : "unknown/absent")); }
            catch (Exception error) { Log("BCD read unavailable: " + error.Message); }
            Log("No microphone, default audio route, certificate or boot setting changed by inspection.");
        }
        ManagementObject CurrentBootObject()
        {
            ManagementScope scope = new ManagementScope(@"\\.\root\WMI");
            scope.Options.EnablePrivileges = true; scope.Options.Impersonation = ImpersonationLevel.Impersonate;
            scope.Connect();
            using (ManagementClass type = new ManagementClass(scope, new ManagementPath("BcdStore"), null))
            using (ManagementBaseObject input = type.GetMethodParameters("OpenStore"))
            {
                input["File"] = "";
                using (ManagementBaseObject result = type.InvokeMethod("OpenStore", input, null))
                    if (!(bool)result["ReturnValue"]) throw new InvalidOperationException("Cannot open guest system BCD");
            }
            using (ManagementObject store = new ManagementObject(scope, new ManagementPath("BcdStore.FilePath=\"\""), null))
            using (ManagementBaseObject input = store.GetMethodParameters("OpenObject"))
            {
                // Microsoft's BCD sample GUID_CURRENT_BOOT_ENTRY, not a guessed boot loader.
                input["Id"] = "{fa926493-6f1c-4193-a414-58f0b2456d1e}";
                using (ManagementBaseObject result = store.InvokeMethod("OpenObject", input, null))
                {
                    if (!(bool)result["ReturnValue"]) throw new InvalidOperationException("Cannot open current guest boot entry");
                    ManagementBaseObject entry = (ManagementBaseObject)result["Object"];
                    if (Convert.ToUInt32(entry["Type"]) != 0x10200003) throw new InvalidOperationException("Current entry is not a Windows loader");
                    string id = Convert.ToString(entry["Id"]);
                    Guid parsed; if (!Guid.TryParse(id, out parsed)) throw new InvalidOperationException("Invalid current boot identifier");
                    return new ManagementObject(scope, new ManagementPath("BcdObject.Id=\"" + parsed.ToString("B") + "\",StoreFilePath=\"\""), null);
                }
            }
        }
        bool? ReadTestMode()
        {
            using (ManagementObject entry = CurrentBootObject())
            using (ManagementBaseObject input = entry.GetMethodParameters("GetElement"))
            {
                input["Type"] = TestSigningElement;
                using (ManagementBaseObject result = entry.InvokeMethod("GetElement", input, null))
                {
                    if (!(bool)result["ReturnValue"]) return null;
                    return (bool)((ManagementBaseObject)result["Element"])["Boolean"];
                }
            }
        }
        public void PrepareTestMode()
        {
            RequireAdministrator(); VerifyPackage();
            using (X509Certificate2 certificate = Certificate())
            {
                foreach (StoreName name in new StoreName[] { StoreName.Root, StoreName.TrustedPublisher })
                using (X509Store store = new X509Store(name, StoreLocation.LocalMachine))
                {
                    RequireTarget(); store.Open(OpenFlags.ReadWrite); store.Add(certificate);
                    Log("Guest public test certificate added to " + name + "; thumbprint=" + certificate.Thumbprint);
                }
            }
            RequireAdministrator();
            using (ManagementObject entry = CurrentBootObject())
            using (ManagementBaseObject input = entry.GetMethodParameters("SetBooleanElement"))
            {
                input["Type"] = TestSigningElement; input["Boolean"] = true;
                using (ManagementBaseObject result = entry.InvokeMethod("SetBooleanElement", input, null))
                    if (!(bool)result["ReturnValue"]) throw new InvalidOperationException("Guest BCD test-signing change failed");
            }
            if (ReadTestMode() != true) throw new InvalidOperationException("Guest BCD write not confirmed");
            Log("Guest BCD TESTSIGNING=true verified. Restart this guest before driver installation. Integrity-check disabling is not used.");
        }
        public void Install(IntPtr owner)
        {
            RequireAdministrator(); VerifyPackage();
            if (!Native.TestSigningActive()) throw new InvalidOperationException("Guest kernel test-signing is not active; restart after setup");
            using (X509Certificate2 expected = Certificate())
            using (X509Certificate2 signer = new X509Certificate2(X509Certificate.CreateFromSignedFile(Path.Combine(DirectoryPath, "RemonteVoiceAudio.sys"))))
                if (expected.Thumbprint != signer.Thumbprint) throw new InvalidOperationException("Wrong driver signer");
            Native.VerifySignature(Path.Combine(DirectoryPath, "RemonteVoiceAudio.sys"));
            Native.VerifySignature(Path.Combine(DirectoryPath, "RemonteVoiceAudio.cat"));
            RequireTarget();
            Native.InstallRootDevice(owner, Path.Combine(DirectoryPath, "RemonteVoiceAudio.inf"), Log);
        }
        public void Reboot() { RequireAdministrator(); Log("User requested guest reboot."); Native.Reboot(); }
    }
}
