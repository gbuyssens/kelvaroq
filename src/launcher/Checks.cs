using System;
using System.Collections.Generic;
using System.IO;
using System.IO.Compression;
using System.IO.Pipes;
using System.Linq;
using System.Text;
using System.Threading;

namespace Velkin {
internal static class Checks {
    static void Check(bool ok) { if (!ok) throw new InvalidOperationException("Assertion failed"); }
    static void HardwareProfiles(string home,Preferences prefs) {
        var locale = new Locale("en");
        var machine = Hardware.Sample();
        var gpu = machine.Preferred;
        Func<int?[]> values = () => ProfileRule.Rules.Select(r => r.Value(machine,prefs)).ToArray();
        Check(!prefs.customProfile && gpu.VendorName == "NVIDIA" && !machine.Warnings(locale).Any());
        Check(values().SequenceEqual(new int?[]{192,1024,96,8,0}));

        gpu.name = "GeForce RTX 3060"; gpu.vram = 12L << 30;
        machine.physical = 8; machine.logical = 16; machine.ram = (16L << 30) - (128L << 20);
        Check(!machine.Warnings(locale).Any() && values().SequenceEqual(new int?[]{128,512,64,4,0}));

        gpu.name = "Radeon RX 7900 XTX"; gpu.vendor = 0x1002; gpu.vram = 24L << 30;
        gpu.wave_min = 32; gpu.wave_max = 64; machine.ram = 32L << 30;
        prefs.customProfile = true; prefs.profileValues["VICTORY_WAVE64_PAIRED"] = 1;
        Check(gpu.VendorName == "AMD" && values().SequenceEqual(new int?[]{192,1024,96,4,null}));

        gpu.name = "Arc A770"; gpu.vendor = 0x8086; gpu.vram = 16L << 30;
        gpu.wave_min = 8; gpu.wave_max = 32;
        Check(gpu.VendorName == "Intel" && values().SequenceEqual(new int?[]{192,1024,96,4,1}));
        prefs.profileValues["VICTORY_WIDE_THREADS"] = 2;
        prefs.profileValues["VICTORY_TEXTURE_STAGE_MB"] = -4;
        prefs.profileValues["VICTORY_UNRECOGNIZED_TEST_KEY"] = 1;
        prefs.Write(home); prefs = Preferences.Load(home);
        var child = new System.Diagnostics.ProcessStartInfo();
        child.EnvironmentVariables.Clear();
        child.EnvironmentVariables["VICTORY_CARD_BUFFERS_MB"] = "700";
        Product.Configure(child,new Installation { Root = home },prefs,locale,false,machine);
        Check(child.EnvironmentVariables["VICTORY_CARD_BUFFERS_MB"] == "700");
        Check(child.EnvironmentVariables["VICTORY_TEXTURE_STAGE_MB"] == "192");
        Check(child.EnvironmentVariables["VICTORY_WAVE64_PAIRED"] == "1");
        Check(child.EnvironmentVariables["VICTORY_WIDE_THREADS"] == "2");
        Check(!child.EnvironmentVariables.ContainsKey("VICTORY_UNRECOGNIZED_TEST_KEY"));
        prefs.customProfile = false;
        Check(values()[3] == 4 && values()[4] == 0);
        var integrated = new Gpu { name = "Integrated GPU", fl12 = true, uma = true,
            sm66 = true, mesh = false, bindless = true, root11 = true };
        machine.gpus = new[]{integrated}; machine.ram = 8L << 30;
        Check(machine.Warnings(locale).Contains(locale.T("missingHardware",locale.T("meshShaders"))));
        Check(machine.Warnings(locale).Contains(locale.T("lowRam")) && values().All(v => v == null));
        machine.gpus = new[]{integrated,gpu};
        Check(machine.Preferred == gpu && machine.Warnings(locale).Contains(locale.T("hybridGpu")));
        Check(values().SequenceEqual(new int?[]{64,256,32,4,0}));
        machine.gpus = new[]{gpu}; gpu.vram = 6L << 30;
        Check(values().SequenceEqual(new int?[]{64,256,32,4,0}));
        gpu.sm66 = null;
        Check(machine.Warnings(locale).Contains(locale.T("unverifiedHardware","Shader Model 6.6")));
        gpu.sm66 = true;
        var old = Environment.GetEnvironmentVariable("VICTORY_LAUNCHER_PROFILES");
        var oldDetection = Environment.GetEnvironmentVariable("VICTORY_LAUNCHER_HARDWARE");
        try {
            foreach (var key in new[]{"VICTORY_LAUNCHER_PROFILES","VICTORY_LAUNCHER_HARDWARE"}) {
                Environment.SetEnvironmentVariable(key,"0");
                child.EnvironmentVariables.Clear(); ProfileRule.Apply(child,machine,prefs);
                Check(child.EnvironmentVariables.Count == 0);
                Environment.SetEnvironmentVariable(key,null);
            }
        } finally {
            Environment.SetEnvironmentVariable("VICTORY_LAUNCHER_PROFILES",old);
            Environment.SetEnvironmentVariable("VICTORY_LAUNCHER_HARDWARE",oldDetection);
        }
    }
    static void Reject(Action action) {
        try { action(); } catch (Exception ex) {
            if (ex is Problem || ex is InvalidDataException || ex is IOException || ex is ArgumentException) return;
            throw;
        }
        throw new InvalidOperationException("Unexpected success");
    }
    static void Discord(string root,Preferences p) {
        const string id = "123456789012345678";
        Check(Presence.ValidAppId(id) && !Presence.ValidAppId("1234") && !Presence.ValidAppId("12345678901234567a") && !Presence.ValidAppId(null)
            && !Presence.ValidAppId("\u0661\u0662\u0663\u0664\u0665\u0666\u0667\u0668\u0669\u0661\u0662\u0663\u0664\u0665\u0666\u0667\u0668"));
        p.discordAppId = "not a number"; Check(Presence.AppId(p) == Presence.AppId(new Preferences()));
        p.discordAppId = id; Check(Presence.AppId(p) == id && Presence.Active(p));
        p.discord = false; Check(!Presence.Active(p)); p.discord = true;
        var frame = Presence.Frame(3,"{\"\u00e9\":1}"); var parsed = Presence.Read(new MemoryStream(frame));
        Check(parsed.Key == 3 && parsed.Value == "{\"\u00e9\":1}" && BitConverter.ToInt32(frame,4) == frame.Length - 8);
        var en = new Locale("en");
        Func<string,bool,string> details = (title,show) => {
            var command = Files.Json.Deserialize<Dictionary<string,object>>(Presence.Activity(title,show,en,42,7));
            var args = (Dictionary<string,object>)command["args"]; Check((string)command["cmd"] == "SET_ACTIVITY" && (int)args["pid"] == 7);
            return (string)((Dictionary<string,object>)args["activity"])["details"];
        };
        Check(details("Sample Game",true) == "Sample Game" && details("Sample Game",false) == en.T("discordHidden") && details(new string('x',300),true).Length == 128);

        string config = Path.Combine(root,"MicrosoftGame.Config"), exe = Path.Combine(root,"bin","sample.exe");
        Directory.CreateDirectory(Path.GetDirectoryName(exe)); File.WriteAllBytes(exe,new byte[0]);
        File.WriteAllText(config,"<Game><ShellVisuals DefaultDisplayName='Sample Game'/><ExecutableList><Executable Name='bin/sample.exe'/></ExecutableList></Game>");
        Check(Installation.Select(exe).Title == "Sample Game");
        File.WriteAllText(config,"<Game><ShellVisuals DefaultDisplayName='ms-resource:Title'/><ExecutableList><Executable Name='bin/sample.exe'/></ExecutableList></Game>");
        Check(Installation.Select(exe).Title == new DirectoryInfo(root).Name);

        // A stand-in for the Discord client: handshake, READY, then the activity, then the clearing on exit.
        Presence.PipePrefix = "velkin-test-" + Guid.NewGuid().ToString("N") + "-";
        var frames = new List<KeyValuePair<int,string>>();
        using (var server = new NamedPipeServerStream(Presence.PipePrefix + "0",PipeDirection.InOut,1,PipeTransmissionMode.Byte,PipeOptions.Asynchronous)) {
            var fake = new Thread(delegate() {
                try {
                    server.WaitForConnection();
                    frames.Add(Presence.Read(server));
                    Presence.Write(server,1,"{\"cmd\":\"DISPATCH\",\"evt\":\"READY\"}");
                    frames.Add(Presence.Read(server));
                    Presence.Write(server,1,"{\"cmd\":\"SET_ACTIVITY\",\"evt\":null}");
                    while (true) frames.Add(Presence.Read(server));
                } catch (Exception) { }
            }) { IsBackground = true };
            fake.Start();
            var presence = Presence.Begin(p,new Installation { Root = root, Title = "Sample Game" },en);
            for (int i = 0; i < 100 && !presence.Shown; i++) Thread.Sleep(50);
            Check(presence.Shown);
            presence.Dispose();
            Check(fake.Join(5000));
        }
        Check(frames.Count == 3 && frames[0].Key == 0 && frames[0].Value.Contains(id) && frames[1].Value.Contains("Sample Game")
            && frames[2].Value.Contains("\"activity\":null"));
        p.discord = false; Check(!Presence.Begin(p,new Installation { Root = root, Title = "x" },en).Shown);
    }
    internal static void Run(string suite,string root) {
        string home = Path.Combine(root,"preferences"), history = Path.Combine(home,"snapshots");
        var p = new Preferences { data = Path.Combine(root,"data"), cache = Path.Combine(root,"cache") };
        Directory.CreateDirectory(home);
        switch (suite) {
            case "hardware":
                HardwareProfiles(home,p);
                break;
            case "releases":
                const string fixture = "{\"tag_name\":\"v0.10.1\",\"body\":\"## Changes\\r\\n- Faster startup\",\"html_url\":\"https://example.invalid/\",\"draft\":false,\"prerelease\":false}";
                var release = Release.Parse(fixture);
                Check(release.body == "Changes Faster startup" && release.Page == "https://github.com/gbuyssens/velkin/releases/tag/v0.10.1");
                Check(release.NewerThan("0.2.9") && !release.NewerThan("0.10.1") && !release.NewerThan("1.0.0"));
                foreach (string tag in new[]{"0.3.0","v0.3","v0.03.0","v0.3.0-beta","v1.2.3.4","v1.2.3\n","v0.2.0/other"})
                    Check(Release.Number(tag) == null);
                Reject(() => Release.Parse("{}")); Reject(() => Release.Parse("{"));
                Reject(() => Release.Parse(fixture.Replace("\"draft\":false","\"draft\":true")));
                Reject(() => Release.Parse(fixture.Replace("\"prerelease\":false","\"prerelease\":true")));
                Check(Release.Parse("{\"tag_name\":\"v1.0.0\",\"body\":null}").body == "");
                break;
            case "updatePolicy":
                var clock = new DateTime(2026,1,1,12,0,0,DateTimeKind.Utc);
                var check = ReleaseCheck.Load(home);
                Check(p.updates);
                p.updates = false; p.Write(home);
                Check(!check.Begin(Preferences.Load(home).updates,clock,() => { throw new Exception(); }) && check.attempted == 0);
                Check(check.Begin(true,clock,() => check.Write(home)));
                check = ReleaseCheck.Load(home);
                Check(!check.Begin(true,clock.AddDays(1).AddTicks(-1),() => { throw new Exception(); }));
                Check(check.Begin(true,clock.AddDays(1),() => check.Write(home)));
                check.latest = Release.Parse("{\"tag_name\":\"v0.3.1\",\"body\":\"Summary\"}"); check.succeeded = true; check.Write(home);
                Check(ReleaseCheck.Load(home).latest.NewerThan("0.2.0"));
                break;
            case "engine":
                var legacy = Path.Combine(root,"victory.exe"); var branded = Path.Combine(root,"velkin-engine.exe");
                File.WriteAllText(legacy,"old"); Check(Product.EngineIn(root) == legacy);
                File.WriteAllText(branded,"new"); Check(Product.EngineIn(root) == branded);
                break;
            case "language":
                var english = new Locale("en").Strings;
                foreach (string code in Locale.Codes) {
                    var table = new Locale(code).Strings;
                    Check(table.Count == english.Count && english.Keys.All(k => table.ContainsKey(k) && !String.IsNullOrWhiteSpace(table[k])));
                    foreach (var key in english.Keys) foreach (var placeholder in new[]{"{0}","{1}","{2}"})
                        Check(english[key].Contains(placeholder) == table[key].Contains(placeholder));
                }
                Check(new Locale("fr-CA").Code == "fr"); Check(new Locale("pt-PT").Code == "pt-BR"); break;
            case "settings":
                p.Write(home); var file = Path.Combine(home,"settings");
                var json = Files.Json.Deserialize<Dictionary<string,object>>(File.ReadAllText(file));
                json["schema_version"] = 1; json.Remove("days"); json.Remove("recent"); json.Remove("updates");
                File.WriteAllText(file,Files.Json.Serialize(json));
                var migrated = Preferences.Load(home);
                Check(migrated.schema_version == 2 && migrated.days == 14 && migrated.recent == 8 && migrated.data == p.data && migrated.updates);
                Check(Preferences.Load(home).schema_version == 2);
                json["schema_version"] = 17; File.WriteAllText(file,Files.Json.Serialize(json)); Reject(() => Preferences.Load(home));
                p.cache = p.data; Reject(() => p.Write(home)); break;
            case "paths":
                string config = Path.Combine(root,"MicrosoftGame.Config"), exe = Path.Combine(root,"bin","sample.exe");
                Directory.CreateDirectory(Path.GetDirectoryName(exe)); File.WriteAllBytes(exe,new byte[0]);
                Reject(() => Installation.Select(exe)); Reject(() => Installation.Select(root));
                File.WriteAllText(config,"<Game><ExecutableList><!-- <Executable Name='ignored.exe'/> --><Executable Name='bin/sample.exe' TargetDeviceFamily='Scarlett'/></ExecutableList></Game>",Encoding.Unicode);
                Check(Installation.Select(root).Image == exe); Check(Installation.Select(exe).Root == root);
                var other = Path.Combine(root,"other.exe"); File.WriteAllText(other,""); Reject(() => Installation.Select(other));
                File.WriteAllText(config,"<Game><ExecutableList><Executable Name='../outside.exe'/></ExecutableList></Game>"); Reject(() => Installation.Select(root));
                File.WriteAllText(config,"<"); Reject(() => Installation.Select(root)); break;
            case "retention":
                DateTime now = new DateTime(2026,11,10,16,0,0,DateTimeKind.Utc);
                var samples = new[]{0,1,2,24,25,48,72}.Select(h => new Snapshot { Info = new Manifest { created_at = now.AddHours(-h).ToString("o") } }).ToList();
                Check(Archives.Expired(samples,now,2,3).SequenceEqual(new[]{samples[2],samples[4],samples[6]}));
                Check(Archives.Expired(samples,now,1,0).Count() == 6); break;
            case "move":
                Directory.CreateDirectory(p.cache); File.WriteAllText(Path.Combine(p.cache,"one"),"original");
                var dest = Path.Combine(root,"target");
                Reject(() => Relocation.Move(p.cache,dest,n => {},delegate { throw new IOException(); }));
                Check(!Directory.Exists(dest) && Files.Size(p.cache) == 8);
                Reject(() => Relocation.Move(p.cache,dest,n => {},useNew => {
                    if (useNew) File.WriteAllText(Path.Combine(p.cache,"changed"),"new");
                }));
                Check(!Directory.Exists(dest) && File.Exists(Path.Combine(p.cache,"changed")));
                int last = 0; Relocation.Move(p.cache,dest,n => last = n,delegate {});
                Check(last == 100 && !Directory.Exists(p.cache) && File.ReadAllText(Path.Combine(dest,"one")) == "original"); break;
            case "clear":
                foreach (var name in new[]{"shaders","pipelines"}) {
                    var dir = Path.Combine(p.cache,name); Directory.CreateDirectory(dir); File.WriteAllText(Path.Combine(dir,"entry"),"cache");
                }
                Files.ClearShaders(p.cache); Check(!Directory.Exists(Path.Combine(p.cache,"shaders")) && Files.Size(p.cache) == 5);
                Directory.CreateDirectory(p.data); File.WriteAllText(Path.Combine(p.data,"slot"),"saved");
                Archives.ClearData(p,history,"def"); Check(Files.Size(p.data) == 0);
                Archives.Restore(Archives.History(history).Single().Path,p,history,"def");
                Check(File.ReadAllText(Path.Combine(p.data,"slot")) == "saved");
                break;
            case "options":
                var info = new System.Diagnostics.ProcessStartInfo { UseShellExecute = false };
                var game = new Installation { Image = Path.Combine(root,"bin","sample.exe"), Root = root };
                p.monitor = true; p.preload = false;
                Product.Configure(info,game,p,new Locale("en"),true);
                Check(info.EnvironmentVariables["VICTORY_GAME_DIR"] == root && info.EnvironmentVariables["VICTORY_CACHE_DIR"] == p.cache &&
                    info.EnvironmentVariables["VICTORY_DATA_DIR"] == p.data && info.EnvironmentVariables["VICTORY_PIPELINE_PRECOMPILE"] == "0" &&
                    info.EnvironmentVariables["VICTORY_LAUNCHER_MONITOR"] == "1" && info.EnvironmentVariables["VICTORY_STATS_SHM"] == "1");
                break;
            case "session":
                Directory.CreateDirectory(p.data);
                string state = Path.Combine(p.data,"slot");
                File.WriteAllText(state,"before");
                Check(Session.Run(p,history,"abc",true,delegate {
                    Check(Archives.History(history).Single().Info.reason == "beforeLaunch");
                    File.WriteAllText(state,"after"); return 0;
                }) == 0);
                Check(Archives.History(history).Count == 2 && Archives.History(history)[0].Info.reason == "afterExit");
                Check(Session.Run(p,history,"abc",true,delegate { return 2; }) == 2);
                Check(Archives.History(history).Count == 3);
                Session.Run(p,history,"abc",false,delegate { return 0; });
                Check(Archives.History(history).Count == 3);
                break;
            case "recovery":
                Directory.CreateDirectory(history);
                var token = Guid.NewGuid().ToString("N");
                var prefix = Path.Combine(root,".velkin-" + token);
                Directory.CreateDirectory(prefix + ".previous");
                File.WriteAllText(Path.Combine(prefix + ".previous","save"),"last good");
                Directory.CreateDirectory(prefix + ".incoming");
                File.WriteAllText(Path.Combine(history,"replacement.json"),Files.Json.Serialize(new Archives.Replacement { token = token, destination = p.data }));
                Archives.Recover(p.data,history); Archives.Recover(p.data,history);
                Check(File.ReadAllText(Path.Combine(p.data,"save")) == "last good" && !Directory.Exists(prefix + ".incoming"));
                break;
            case "archive":
                Directory.CreateDirectory(Path.Combine(p.data,"empty")); string save = Path.Combine(p.data,"save");
                File.WriteAllText(save,"first");
                string snapshot = Archives.Write(p.data,history,"abc","manual","sample");
                string export = Path.Combine(root,"export.zip"); Archives.Export(snapshot,export);
                File.WriteAllText(save,"second"); Archives.Restore(export,p,history,"def");
                Check(File.ReadAllText(save) == "first" && Directory.Exists(Path.Combine(p.data,"empty")));
                var backup = Archives.History(history).Single(s => s.Info.reason == "beforeRestore");
                Archives.Restore(backup.Path,p,history,"def"); Check(File.ReadAllText(save) == "second");
                foreach (string fault in new[]{"version","checksum","traversal","missing","corrupt"}) {
                    var damaged = Path.Combine(root,fault + ".zip"); File.Copy(export,damaged);
                    if (fault == "corrupt") File.WriteAllText(damaged,"invalid");
                    else using (var stream = File.Open(damaged,FileMode.Open)) using (var zip = new ZipArchive(stream,ZipArchiveMode.Update)) {
                        var e = zip.GetEntry("manifest.json"); Manifest m;
                        using (var r = new StreamReader(e.Open())) m = Files.Json.Deserialize<Manifest>(r.ReadToEnd());
                        e.Delete();
                        if (fault == "version") m.format_version = 0;
                        if (fault == "checksum") m.files[0].sha256 = new string('0',64);
                        if (fault == "traversal") m.files[0].path = "../escape";
                        if (fault != "missing") using (var w = new StreamWriter(zip.CreateEntry("manifest.json").Open())) w.Write(Files.Json.Serialize(m));
                    }
                    Reject(() => Archives.Restore(damaged,p,history,"def"));
                    Check(File.ReadAllText(save) == "second" && !File.Exists(Path.Combine(root,"escape")));
                }
                break;
            case "discord":
                Discord(root,p);
                break;
            default: throw new ArgumentException(suite);
        }
    }
}
}
