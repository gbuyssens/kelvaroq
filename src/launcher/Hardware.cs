using Microsoft.Win32;
using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Runtime.InteropServices;
using System.Text;

namespace Velkin {
internal sealed class Gpu {
    public string name { get; set; }
    public int vendor { get; set; }
    public long vram { get; set; }
    public string driver { get; set; }
    public bool fl12 { get; set; }
    public bool? uma { get; set; }
    public bool? sm66 { get; set; }
    public bool? mesh { get; set; }
    public bool? bindless { get; set; }
    public bool? root11 { get; set; }
    public bool? barriers { get; set; }
    public bool? upload_heap { get; set; }
    public int wave_min { get; set; }
    public int wave_max { get; set; }

    internal string VendorName {
        get {
            switch (vendor) {
                case 0x10de: return "NVIDIA";
                case 0x1002: return "AMD";
                case 0x8086: return "Intel";
                default: return "0x" + vendor.ToString("X4");
            }
        }
    }
    internal bool Compatible { get { return fl12 && sm66 == true && mesh == true && bindless == true && root11 == true; } }
}

internal sealed class Hardware {
    public Gpu[] gpus { get; set; }
    public int physical { get; set; }
    public int logical { get; set; }
    public long? ram { get; set; }
    internal string Cpu, Build;
    internal static bool Enabled { get { return Product.Enabled("VICTORY_LAUNCHER_HARDWARE"); } }

    // DXGI supplied the preference order. Preserve it within an architecture class
    // and use the UMA query to distinguish integrated adapters with reserved VRAM.
    internal Gpu Preferred {
        get { return gpus == null ? null : gpus.OrderByDescending(g => g.fl12)
            .ThenByDescending(g => g.uma == false).FirstOrDefault(); }
    }
    internal bool Multiple { get { return gpus != null && gpus.Length > 1; } }

    [DefaultDllImportSearchPaths(DllImportSearchPath.ApplicationDirectory | DllImportSearchPath.System32)]
    [DllImport("velkin_simd.dll", ExactSpelling = true)]
    static extern UIntPtr velkin_hardware([Out] byte[] buffer, UIntPtr capacity);

    static string ReadName(string key,string value) {
        try { return Convert.ToString(Registry.GetValue(key,value,null)).Trim(); }
        catch (System.Security.SecurityException) { return null; }
        catch (UnauthorizedAccessException) { return null; }
        catch (System.IO.IOException) { return null; }
    }
    internal static Hardware Detect() {
        var machine = new Hardware();
        try {
            var bytes = new byte[64 * 1024];
            int size = checked((int)velkin_hardware(bytes,(UIntPtr)bytes.Length).ToUInt64());
            if (size > 0) machine = Files.Json.Deserialize<Hardware>(Encoding.UTF8.GetString(bytes,0,size));
        } catch (DllNotFoundException) {}
          catch (EntryPointNotFoundException) {}
          catch (BadImageFormatException) {}
        machine.Cpu = ReadName(@"HKEY_LOCAL_MACHINE\HARDWARE\DESCRIPTION\System\CentralProcessor\0","ProcessorNameString");
        machine.Build = ReadName(@"HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Windows NT\CurrentVersion","CurrentBuildNumber");
        return machine;
    }

    internal static long GiB(long bytes) { return (bytes + (1L << 29)) >> 30; }
    internal IEnumerable<string> Warnings(Locale locale) {
        var g = Preferred;
        if (g == null) yield return locale.T("noGpu");
        else if (!g.fl12) yield return locale.T("missingHardware","Direct3D 12 FL 12.0");
        else {
            var missing = new List<string>(); var unknown = new List<string>();
            foreach (var item in new Dictionary<string,bool?> {
                {"Shader Model 6.6",g.sm66}, {locale.T("meshShaders"),g.mesh},
                {"Resource Binding Tier 3",g.bindless}, {"Root Signature 1.1",g.root11}
            }) {
                if (item.Value == false) missing.Add(item.Key);
                if (!item.Value.HasValue) unknown.Add(item.Key);
            }
            if (missing.Count > 0) yield return locale.T("missingHardware",String.Join(", ",missing));
            if (unknown.Count > 0) yield return locale.T("unverifiedHardware",String.Join(", ",unknown));
        }
        if (ram.HasValue && GiB(ram.Value) < 16) yield return locale.T("lowRam");
        if (Multiple) yield return locale.T("hybridGpu");
    }

    internal static Hardware Sample() {
        var gpu = new Gpu { name = "NVIDIA GeForce RTX 5080", vendor = 0x10de, vram = 16L << 30,
            driver = "32.0.15.0000", fl12 = true, uma = false, sm66 = true, mesh = true,
            bindless = true, root11 = true, wave_min = 32, wave_max = 32, barriers = true, upload_heap = true };
        return new Hardware { gpus = new[]{gpu}, Cpu = "AMD Ryzen 9 7950X", physical = 16, logical = 32,
            ram = 64L << 30, Build = "26100" };
    }
}

/// The public engine manages texture residency against the live DXGI budget.
/// Its pipeline library has no configurable memory limit. These rules size only
/// bounded work or caches whose public implementation has a fallback.
internal sealed class ProfileRule {
    internal readonly string Key, Label;
    internal readonly int[] Choices;
    internal readonly Func<Hardware,int?> Automatic;
    ProfileRule(string key,string label,int[] choices,Func<Hardware,int?> automatic) {
        Key = "VICTORY_" + key; Label = label; Choices = choices; Automatic = automatic;
    }

    static int? Tier(Hardware h,int small,int medium,int large) {
        var g = h.Preferred;
        if (g.uma != false || g.vram == 0 || !h.ram.HasValue) return null;
        long video = Hardware.GiB(g.vram), system = Hardware.GiB(h.ram.Value);
        if (video >= 16 && system >= 32) return large;
        if (video >= 10 && system >= 16) return medium;
        return small;
    }

    internal static readonly ProfileRule[] Rules = {
        // The engine schedules unfinished texture work on later frames. Limit the
        // upload burst on smaller machines and retain its 192 MiB ceiling on larger ones.
        new ProfileRule("TEXTURE_STAGE_MB","textureStage",new[]{64,128,192},h => Tier(h,64,128,192)),
        // Writable card buffers may also need a second copy. Keep at most the
        // public 1 GiB default and leave the dynamic DXGI residency check in place.
        new ProfileRule("CARD_BUFFERS_MB","cardBuffers",new[]{256,512,1024},h => Tier(h,256,512,1024)),
        // Landing buffers can be reused after their fence. Public runs uploaded
        // 45–76 MiB per frame. When full, the engine uses the direct queue.
        new ProfileRule("QUEUE2_LANDING_MB","landingCache",new[]{32,64,96},h => Tier(h,32,64,96)),
        // Copy and compare workers share cores with the title and submission.
        // Use half the physical cores, up to the public default ceiling of twelve.
        new ProfileRule("WIDE_THREADS","copyWorkers",Enumerable.Range(1,12).ToArray(),h =>
            h.physical > 0 ? (int?)Math.Max(1,Math.Min(12,h.physical / 2)) : null),
        // Patch 0052 leaves pairing experimental. Offer it only in Custom when
        // 32 lanes are supported and the device cannot run a native wave of 64.
        new ProfileRule("WAVE64_PAIRED","pairedWaves",new[]{0,1},h => {
            var g = h.Preferred;
            return g.wave_min > 0 && g.wave_min <= 32 && g.wave_max == 32 ? (int?)0 : null;
        })
    };

    internal int? Value(Hardware h,Preferences prefs) {
        if (h == null || h.Preferred == null || !h.Preferred.Compatible) return null;
        var value = Automatic(h);
        int custom;
        if (value.HasValue && prefs.customProfile && prefs.profileValues != null &&
            prefs.profileValues.TryGetValue(Key,out custom) && Choices.Contains(custom)) return custom;
        return value;
    }

    internal static bool Enabled { get { return Hardware.Enabled && Product.Enabled("VICTORY_LAUNCHER_PROFILES"); } }
    internal static void Apply(System.Diagnostics.ProcessStartInfo start,Hardware hardware,Preferences prefs) {
        if (!Enabled) return;
        foreach (var rule in Rules) {
            var value = rule.Value(hardware,prefs);
            if (value.HasValue && !start.EnvironmentVariables.ContainsKey(rule.Key))
                start.EnvironmentVariables[rule.Key] = value.Value.ToString(CultureInfo.InvariantCulture);
        }
    }
}
}
