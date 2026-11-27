using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.IO.Compression;
using System.Linq;
using System.Text;

namespace Velkin {
internal sealed class SavedFile {
    public string path { get; set; }
    public long size { get; set; }
    public string sha256 { get; set; }
}
internal sealed class Manifest {
    public int format_version { get; set; }
    public string launcher_version { get; set; }
    public string engine_build { get; set; }
    public string created_at { get; set; }
    public string label { get; set; }
    public string reason { get; set; }
    public List<SavedFile> files { get; set; }
    public List<string> directories { get; set; }
}
internal sealed class Snapshot {
    internal string Path;
    internal Manifest Info;
    internal bool Damaged;
    internal DateTime Time { get { return DateTime.ParseExact(Info.created_at,"o",CultureInfo.InvariantCulture,DateTimeStyles.RoundtripKind).ToUniversalTime(); } }
}

internal static class Archives {
    const long MaxBytes = 64L * 1024 * 1024 * 1024;
    internal static string Write(string folder, string history, string build, string reason, string label) {
        Files.Separate(folder,history);
        Directory.CreateDirectory(history);
        var now = DateTime.UtcNow;
        var m = new Manifest { format_version = 1, launcher_version = Product.Version, engine_build = build,
            created_at = now.ToString("o"), reason = reason, label = label, files = new List<SavedFile>(), directories = new List<string>() };
        string temp = Files.Unique(history), final = Path.Combine(history,now.ToString("yyyyMMdd-HHmmss-fffffff") + "-" + Guid.NewGuid().ToString("N") + ".zip");
        try {
            using (var disk = new FileStream(temp,FileMode.CreateNew))
            {
                using (var archive = new ZipArchive(disk,ZipArchiveMode.Create,true)) {
                    foreach (string path in Files.Tree(folder)) {
                        string rel = path.Substring(folder.TrimEnd('\\').Length + 1).Replace('\\','/');
                        ValidateName(rel);
                        if (Directory.Exists(path)) { m.directories.Add(rel); continue; }
                        var entry = archive.CreateEntry("data/" + rel,CompressionLevel.Fastest);
                        using (var input = File.OpenRead(path))
                        using (var output = entry.Open()) {
                            var item = new SavedFile { path = rel, size = input.Length, sha256 = Files.Hash(input) };
                            input.Position = 0; input.CopyTo(output); m.files.Add(item);
                        }
                    }
                    using (var w = new StreamWriter(archive.CreateEntry("manifest.json").Open(),new UTF8Encoding(false))) w.Write(Files.Json.Serialize(m));
                }
                disk.Flush(true);
            }
            Inspect(temp,null); File.Move(temp,final);
            return final;
        } finally { File.Delete(temp); }
    }
    static void ValidateName(string rel) {
        if (String.IsNullOrEmpty(rel) || rel.Contains('\\')) throw new Problem("unsafePath");
        foreach (string part in rel.Split('/')) Files.ValidPart(part);
    }
    static Manifest Header(ZipArchive z) {
        var e = z.GetEntry("manifest.json");
        if (e == null || e.Length > 8 * 1024 * 1024) throw new Problem("badArchive");
        Manifest m;
        // Enforce the declared limit while reading as well as in the ZIP directory.
        using (var stream = e.Open()) {
            byte[] raw = new byte[(int)e.Length];
            for (int pos = 0; pos < raw.Length;) {
                int count = stream.Read(raw,pos,raw.Length - pos);
                if (count == 0) throw new Problem("badArchive");
                pos += count;
            }
            if (stream.ReadByte() != -1) throw new Problem("badArchive");
            m = Files.Json.Deserialize<Manifest>(new UTF8Encoding(false,true).GetString(raw).TrimStart('\uFEFF'));
        }
        if (m == null || m.format_version != 1) throw new Problem("archiveVersion");
        DateTime time;
        if (m.files == null || m.directories == null || m.files.Count + m.directories.Count > 100000 ||
            m.files.Any(f => f == null || f.size < 0 || String.IsNullOrEmpty(f.path) || f.sha256 == null || f.sha256.Length != 64) ||
            String.IsNullOrEmpty(m.engine_build) || String.IsNullOrEmpty(m.launcher_version) ||
            !DateTime.TryParseExact(m.created_at,"o",CultureInfo.InvariantCulture,DateTimeStyles.RoundtripKind,out time)) throw new Problem("badArchive");
        return m;
    }
    internal static Manifest Inspect(string file, string stage) {
        using (var disk = File.OpenRead(file))
        using (var z = new ZipArchive(disk,ZipArchiveMode.Read)) {
            var m = Header(z);
            if (z.Entries.Count != m.files.Count + 1) throw new Problem("badArchive");
            var used = new HashSet<string>(Files.Names);
            var entries = z.Entries.ToDictionary(e => e.FullName,StringComparer.Ordinal);
            foreach (var d in m.directories) {
                ValidateName(d);
                if (!used.Add(d)) throw new Problem("badArchive");
                if (stage != null) Directory.CreateDirectory(Path.Combine(stage,d));
            }
            var directories = new HashSet<string>(used,Files.Names);
            foreach (var rel in m.directories.Concat(m.files.Select(f => f == null ? null : f.path))) {
                ValidateName(rel);
                int separator = rel.LastIndexOf('/');
                if (separator >= 0 && !directories.Contains(rel.Substring(0,separator))) throw new Problem("badArchive");
            }
            long total = 0;
            var buffer = new byte[131072];
            foreach (var f in m.files) {
                ValidateName(f.path);
                ZipArchiveEntry e;
                if (!used.Add(f.path) || !entries.TryGetValue("data/" + f.path,out e) || f.size < 0 ||
                    f.size > MaxBytes - total || e.Length != f.size) throw new Problem("badArchive");
                total += f.size;
                var dest = stage == null ? null : Path.Combine(stage,f.path);
                if (dest != null) Directory.CreateDirectory(Path.GetDirectoryName(dest));
                using (var source = e.Open())
                using (var output = dest == null ? Stream.Null : new FileStream(dest,FileMode.CreateNew))
                using (var hash = System.Security.Cryptography.SHA256.Create()) {
                    long written = 0;
                    int n;
                    while ((n = source.Read(buffer,0,buffer.Length)) > 0) {
                        if ((written += n) > f.size) throw new Problem("badArchive");
                        hash.TransformBlock(buffer,0,n,buffer,0); output.Write(buffer,0,n);
                    }
                    hash.TransformFinalBlock(buffer,0,0);
                    if (written != f.size || !String.Equals(BitConverter.ToString(hash.Hash).Replace("-",""),f.sha256,StringComparison.OrdinalIgnoreCase)) throw new Problem("checksum");
                }
            }
            return m;
        }
    }
    internal static List<Snapshot> History(string history) {
        var list = new List<Snapshot>();
        if (Directory.Exists(history)) foreach (string path in Directory.EnumerateFiles(history,"*.zip")) {
            try {
                using (var disk = File.OpenRead(path))
                using (var z = new ZipArchive(disk,ZipArchiveMode.Read)) list.Add(new Snapshot { Path = path, Info = Header(z) });
            } catch (Exception ex) {
                if (!(ex is Problem || ex is InvalidDataException || ex is ArgumentException || ex is InvalidOperationException || ex is IOException)) throw;
                list.Add(new Snapshot { Path = path, Damaged = true, Info = new Manifest {
                    created_at = File.GetLastWriteTimeUtc(path).ToString("o"), reason = "badArchive", files = new List<SavedFile>(),
                    launcher_version = "?", engine_build = "unknown" } });
            }
        }
        return list.OrderByDescending(s => s.Time).ThenByDescending(s => s.Path,StringComparer.Ordinal).ToList();
    }
    internal static IEnumerable<Snapshot> Expired(IEnumerable<Snapshot> snapshots, DateTime now, int recent, int days) {
        int index = 0; var daily = new HashSet<DateTime>();
        foreach (var s in snapshots.OrderByDescending(v => v.Time)) {
            bool keepDay = days > 0 && s.Time.Date > now.Date.AddDays(-days) && daily.Add(s.Time.Date);
            if (++index > recent && !keepDay) yield return s;
        }
    }
    internal static void Prune(string history, Preferences prefs) {
        foreach (var item in Expired(History(history).Where(s => !s.Damaged),DateTime.UtcNow,prefs.recent,prefs.days)) File.Delete(item.Path);
    }
    internal sealed class Replacement {
        public string destination { get; set; }
        public string token { get; set; }
    }
    // A power loss can occur between two directory renames. The marker names only
    // two unique siblings of the selected data directory and is flushed first.
    internal static void Recover(string dest,string history) {
        string marker = Path.Combine(history,"replacement.json");
        if (!File.Exists(marker)) return;
        var pending = Files.Json.Deserialize<Replacement>(File.ReadAllText(marker));
        Guid token;
        if (pending == null || !Files.Names.Equals(Files.Full(dest),pending.destination) ||
            !Guid.TryParseExact(pending.token,"N",out token)) throw new Problem("badSettings");
        string prefix = Path.Combine(Path.GetDirectoryName(dest),".velkin-" + token.ToString("N"));
        string previous = prefix + ".previous", stage = prefix + ".incoming";
        Files.NoLinks(dest); Files.NoLinks(previous); Files.NoLinks(stage);
        if (!Directory.Exists(dest) && Directory.Exists(previous)) Directory.Move(previous,dest);
        // If the new directory is already live, the recovery snapshot also holds the old one.
        Files.RemoveTree(previous); Files.RemoveTree(stage); File.Delete(marker);
    }
    internal static void Restore(string file,Preferences prefs,string history,string build) {
        Replace(prefs,history,build,stage => Inspect(file,stage));
    }
    internal static void ClearData(Preferences prefs,string history,string build) {
        Replace(prefs,history,build,stage => {});
    }
    /// Approach after XCL 2d94699: publish verified contents only after the replacement is complete.
    /// A sibling stage permits a same-volume rename even when the archive is on another drive.
    static void Replace(Preferences prefs,string history,string build,Action<string> prepare) {
        string dest = prefs.ActiveData, parent = Path.GetDirectoryName(dest);
        Files.SafeRoot(dest); Files.Separate(dest,history);
        Recover(dest,history);
        Directory.CreateDirectory(parent);
        var pending = new Replacement { destination = Files.Full(dest), token = Guid.NewGuid().ToString("N") };
        string prefix = Path.Combine(parent,".velkin-" + pending.token);
        string stage = prefix + ".incoming", previous = prefix + ".previous";
        Directory.CreateDirectory(stage);
        try {
            prepare(stage);
            Write(dest,history,build,"beforeRestore",null);
            string marker = Path.Combine(history,"replacement.json"), temp = Files.Unique(history);
            try {
                using (var stream = new FileStream(temp,FileMode.CreateNew)) {
                    var bytes = Encoding.UTF8.GetBytes(Files.Json.Serialize(pending));
                    stream.Write(bytes,0,bytes.Length); stream.Flush(true);
                }
                File.Move(temp,marker);
            } finally { File.Delete(temp); }
            try {
                if (Directory.Exists(dest)) Directory.Move(dest,previous);
                Directory.Move(stage,dest);
            } finally { Recover(dest,history); }
        } finally { Files.RemoveTree(stage); }
        Prune(history,prefs);
    }
    internal static void Export(string source, string dest) {
        if (Files.Names.Equals(Files.Full(source),Files.Full(dest))) throw new Problem("overlap");
        string tmp = Files.Unique(Path.GetDirectoryName(dest));
        try { File.Copy(source,tmp); Inspect(tmp,null); Files.Publish(tmp,dest); }
        finally { File.Delete(tmp); }
    }
}
}
