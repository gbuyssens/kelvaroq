using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Threading;
using System.Windows;

namespace Velkin {
internal sealed class Locale {
    internal static readonly string[] Codes = {"en","fr","es","de","it","pt-BR"};
    internal static readonly string[] Names = {"English","Français","Español","Deutsch","Italiano","Português (Brasil)"};
    internal string Code;
    internal Dictionary<string,string> Strings;
    internal CultureInfo Culture;
    internal Locale(string preference) {
        var language = preference == "system" ? CultureInfo.CurrentUICulture.Name : preference;
        if (!Product.Enabled("VICTORY_LAUNCHER_I18N")) language = "fr";
        Code = Codes.FirstOrDefault(c => language.StartsWith(c.Split('-')[0],StringComparison.OrdinalIgnoreCase)) ?? "en";
        Culture = CultureInfo.CreateSpecificCulture(Code);
        using (var r = new StreamReader(Assembly.GetExecutingAssembly().GetManifestResourceStream(Code + ".json")))
            Strings = Files.Json.Deserialize<Dictionary<string,string>>(r.ReadToEnd());
    }
    internal string T(string key, params object[] args) { return String.Format(Culture,Strings[key],args); }
    internal string Error(Exception ex) { return ex is Problem ? T(ex.Message) : T("failed") + "\n" + ex.Message; }
}
internal static class Product {
    internal static string Version { get { return Assembly.GetExecutingAssembly().GetName().Version.ToString(3); } }
    internal static readonly string Base = AppDomain.CurrentDomain.BaseDirectory;
    internal static bool Enabled(string key) { return Environment.GetEnvironmentVariable(key) != "0"; }
    internal sealed class Patch { public string name { get; set; } }
    internal sealed class Receipt { public string patched_tree { get; set; } public Patch[] patches { get; set; } }
    internal static string EngineIn(string folder) {
        string engine = Path.Combine(folder,"velkin-engine.exe");
        return File.Exists(engine) ? engine : Path.Combine(folder,"victory.exe");
    }
    internal static Receipt Build() {
        var file = Path.Combine(Base,"native","build.json");
        if (File.Exists(file)) return Files.Json.Deserialize<Receipt>(File.ReadAllText(file));
        var receipt = new Receipt();
        var engine = EngineIn(Path.Combine(Base,"native"));
        if (File.Exists(engine)) using (var bytes = File.OpenRead(engine)) receipt.patched_tree = Files.Hash(bytes);
        return receipt;
    }
    internal static bool PreloadAllowed { get { return Enabled("VICTORY_PIPELINE_PRECOMPILE") && Enabled("VICTORY_PIPELINE_WARMUP"); } }
    internal static ProcessStartInfo Command(Installation game, Preferences p, Locale locale, bool experimental, bool dark, Hardware hardware) {
        string cli = Path.Combine(Base,"velkin-cli.exe");
        if (!File.Exists(cli) && Files.Names.Equals(Path.GetFileName(Environment.GetEnvironmentVariable("VICTORY_LAUNCHER_EXE")),"velkin-launcher.exe"))
            cli = Path.Combine(Base,"velkin.exe");
        if (!File.Exists(cli) || !File.Exists(EngineIn(Path.Combine(Base,"native")))) throw new Problem("engineMissing");
        var start = new ProcessStartInfo(cli,"play " + (experimental ? "--experimental-render " : "") + "-- \"" + game.Image + "\"") {
            UseShellExecute = false, CreateNoWindow = true, RedirectStandardError = true, WorkingDirectory = Base };
        Configure(start,game,p,locale,dark,hardware);
        return start;
    }
    internal static void Configure(ProcessStartInfo start,Installation game,Preferences p,Locale locale,bool dark,Hardware hardware = null) {
        ProfileRule.Apply(start,hardware,p);
        start.EnvironmentVariables["VELKIN_DESKTOP_LOG"] = "1";
        start.EnvironmentVariables["VICTORY_LAUNCHER_UI"] = "1";
        start.EnvironmentVariables["VICTORY_GAME_DIR"] = game.Root;
        start.EnvironmentVariables["VICTORY_CACHE_DIR"] = p.ActiveCache;
        start.EnvironmentVariables["VICTORY_DATA_DIR"] = p.ActiveData;
        start.EnvironmentVariables["VICTORY_LAUNCHER_LANGUAGE"] = locale.Code;
        start.EnvironmentVariables["VICTORY_LAUNCHER_DARK"] = dark ? "1" : "0";
        bool monitor = p.monitor && Product.Enabled("VICTORY_LAUNCHER_MONITOR");
        start.EnvironmentVariables["VICTORY_LAUNCHER_MONITOR"] = monitor ? "1" : "0";
        if (monitor) start.EnvironmentVariables["VICTORY_STATS_SHM"] = "1";
        start.EnvironmentVariables["VICTORY_PIPELINE_PRECOMPILE"] = p.preload && PreloadAllowed ? "1" : "0";
    }
}
internal static class Session {
    /// The pre-launch snapshot finishes before the child starts. Only a clean exit creates the second copy.
    internal static int Run(Preferences prefs,string history,string build,bool snapshots,Func<int> play) {
        if (snapshots) Snapshot(prefs,history,build,"beforeLaunch");
        int status = play();
        if (status == 0 && snapshots) Snapshot(prefs,history,build,"afterExit");
        return status;
    }
    static void Snapshot(Preferences prefs,string history,string build,string reason) {
        Archives.Write(prefs.ActiveData,history,build,reason,null);
        Archives.Prune(history,prefs);
    }
}
internal static class App {
    [STAThread] internal static int Main(string[] args) {
        var locale = new Locale("system");
        bool demo = args.Length > 0 && args[0] == "--ui-demo";
        bool test = args.Length == 2 && args[0] == "--self-test";
        string home = demo || test ? Files.Unique(Path.GetTempPath()) :
            Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData),"Velkin");
        try {
            Directory.CreateDirectory(home);
            if (test) { Checks.Run(args[1],home); return 0; }
            FileStream session;
            try { session = new FileStream(Path.Combine(home,"session.lock"),FileMode.OpenOrCreate,FileAccess.ReadWrite,FileShare.None); }
            catch (IOException) { throw new Problem("alreadyOpen"); }
            using (session) {
                var prefs = demo ? new Preferences { data = Path.Combine(home,"sample-data"), cache = Path.Combine(home,"sample-cache") } : Preferences.Load(home);
                locale = new Locale(prefs.language);
                if (!demo) Archives.Recover(prefs.ActiveData,Path.Combine(home,"snapshots"));
                var window = new Shell(home,prefs,demo,args.Contains("--experimental-render"));
                if (demo && args.Length == 3 && args[1] == "--capture") { window.Capture(args[2]); return 0; }
                var selections = args.Where(a => a != "--experimental-render" && a != "--ui-demo").ToArray();
                if (selections.Length > 1) throw new Problem("selectExe");
                if (selections.Length == 1 && !demo) window.Loaded += delegate { window.OpenArgument(selections[0]); };
                new Application().Run(window);
                return window.ExitStatus;
            }
        } catch (Exception ex) {
            if (test || (demo && args.Length > 1)) Console.Error.WriteLine(ex);
            else MessageBox.Show(locale.Error(ex),"Velkin",MessageBoxButton.OK,MessageBoxImage.Error);
            return 1;
        } finally { if (demo || test) Files.RemoveTree(home); }
    }
}
}
