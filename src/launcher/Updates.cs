using System;
using System.IO;
using System.Linq;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.RegularExpressions;

namespace Velkin {
internal sealed class Release {
    public string tag_name { get; set; }
    public string body { get; set; }
    public bool draft { get; set; }
    public bool prerelease { get; set; }

    internal static Version Number(string tag) {
        Version result;
        if (tag == null || tag.Length > 34 ||
            !Regex.IsMatch(tag,@"\Av(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\z") ||
            !Version.TryParse(tag.Substring(1),out result)) return null;
        return result;
    }
    internal static Release Parse(string json) {
        var release = Files.Json.Deserialize<Release>(json);
        if (release == null || release.draft || release.prerelease || Number(release.tag_name) == null)
            throw new InvalidDataException();
        var lines = (release.body ?? "").Split('\n')
            .Select(s => s.Trim().TrimStart('#','-','*',' ').Trim()).Where(s => s.Length > 0);
        string summary = Regex.Replace(String.Join(" ",lines),@"\s+"," ");
        release.body = summary.Length <= 260 ? summary : summary.Substring(0,259).TrimEnd() + "…";
        return release;
    }
    internal bool NewerThan(string installed) {
        var version = Number(tag_name);
        return version != null && version > new Version(installed);
    }
    internal string Page { get { return "https://github.com/gbuyssens/velkin/releases/tag/" + tag_name; } }
}

internal sealed class ReleaseCheck {
    public long attempted { get; set; }
    public bool succeeded { get; set; }
    public Release latest { get; set; }

    internal static ReleaseCheck Load(string home) {
        try {
            return Files.Json.Deserialize<ReleaseCheck>(File.ReadAllText(Path.Combine(home,"latest-release.json"))) ?? new ReleaseCheck();
        } catch (Exception) { return new ReleaseCheck(); }
    }
    internal void Write(string home) {
        string temp = Files.Unique(home);
        try {
            File.WriteAllText(temp,Files.Json.Serialize(this));
            Files.Publish(temp,Path.Combine(home,"latest-release.json"));
        } finally { File.Delete(temp); }
    }
    /// Record the attempt before contacting GitHub. Offline starts and clicks share the same daily limit.
    internal bool Begin(bool enabled,DateTime utc,Action save) {
        if (!enabled || (attempted > 0 && utc.Ticks - attempted < TimeSpan.TicksPerDay)) return false;
        attempted = utc.Ticks;
        succeeded = false;
        save();
        return true;
    }

    [DefaultDllImportSearchPaths(DllImportSearchPath.ApplicationDirectory | DllImportSearchPath.System32)]
    [DllImport("velkin_simd.dll", ExactSpelling = true)]
    static extern uint velkin_latest_release([Out] byte[] buffer,uint length);

    internal static Release Fetch() {
        var data = new byte[512 * 1024];
        int n = (int)velkin_latest_release(data,(uint)data.Length);
        return n == 0 ? null : Release.Parse(new UTF8Encoding(false,true).GetString(data,0,n));
    }
}
}
