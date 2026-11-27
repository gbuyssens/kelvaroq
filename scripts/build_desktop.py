"""Build the desktop companion with the compiler and WPF assemblies supplied by Windows."""
import os
import shutil
import subprocess
import sys
from pathlib import Path

def build(out, version):
    src = Path(__file__).resolve().parents[1] / "src/launcher"
    framework = Path(os.environ["WINDIR"]) / "Microsoft.NET/Framework64/v4.0.30319"
    sdk = Path(os.environ["ProgramFiles(x86)"]) / "Windows Kits/10/bin"
    compilers = sorted(sdk.glob("10.*/x64/rc.exe"), key=lambda p: tuple(map(int, p.parents[1].name.split("."))))
    rc = shutil.which("rc.exe") or compilers[-1]
    nums = ",".join(version.split(".") + ["0"])
    manifest = out / "app.manifest"
    manifest.write_text((src / "app.manifest").read_text().replace("@VERSION@", version), encoding="utf-8")
    resource = (src / "launcher.rc").read_text().replace("@NUMS@", nums).replace("@VERSION@", version)
    (out / "launcher.rc").write_text(resource, encoding="ascii")
    subprocess.run([rc, "/nologo", "/fo", out / "launcher.res", out / "launcher.rc"], check=True)
    # OUT_DIR is <profile>/build/<package>/out for both host and cross builds.
    dest = out.parents[2] / "velkin-desktop.exe"
    assemblies = ["System.Core", "System.Xml.Linq", "System.Web.Extensions", "System.IO.Compression",
                  "System.Windows.Forms", "WPF/PresentationCore", "WPF/PresentationFramework", "WPF/WindowsBase", "System.Xaml"]
    attrs = out / "version.cs"
    attrs.write_text('[assembly:System.Runtime.Versioning.TargetFramework(".NETFramework,Version=v4.8")]\n'
                     '[assembly:System.Reflection.AssemblyVersion("' + version + '.0")]\n'
                     '[assembly:System.Reflection.AssemblyCompany("Velkin contributors")]\n'
                     '[assembly:System.Reflection.AssemblyProduct("Velkin")]\n', encoding="ascii")
    locales = [src / "locales" / (code + ".json") for code in ("en", "fr", "es", "de", "it", "pt-BR")]
    resources = locales + [src / "controls.xaml", src.parents[1] / "assets/launcher/mark.png"]
    subprocess.run([framework / "csc.exe", "/nologo", "/target:winexe", "/platform:x64", "/optimize+",
                    "/utf8output", "/codepage:65001", "/win32manifest:" + str(manifest),
                    "/out:" + str(out / dest.name), *["/reference:" + str(framework / (a + ".dll")) for a in assemblies],
                    *["/resource:" + str(p) + "," + p.name for p in resources],
                    attrs, *sorted(src.glob("*.cs"))], check=True)
    shutil.copyfile(out / dest.name, dest)

if __name__ == "__main__":
    build(Path(sys.argv[1]), sys.argv[2])
