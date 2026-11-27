using System;
using System.Collections.Generic;
using System.IO;
using System.IO.Pipes;
using System.Linq;
using System.Text;
using System.Threading;

namespace Velkin {
/// Discord Rich Presence through the local IPC pipe that the Discord client opens.
/// Best effort only: Discord may be closed, slow or refuse the Application ID, and the game never waits on it.
/// The connection lives on its own background thread for as long as the game runs; closing the pipe clears the activity.
internal sealed class Presence : IDisposable {
    /// The project's own Discord application, so that every user gets the presence without setting anything.
    /// Empty until it has been created on https://discord.com/developers/applications. Settings and VELKIN_DISCORD_APP_ID override it.
    internal const string BuiltInAppId = "";
    internal const string LargeImage = "velkin";
    /// Discord listens on discord-ipc-0 to discord-ipc-9. Tests point this at a pipe of their own.
    internal static string PipePrefix = "discord-ipc-";
    const int MaxFrame = 64 * 1024;

    readonly object gate = new object();
    NamedPipeClientStream pipe;
    Thread worker;
    bool closed;
    /// True once Discord has accepted the activity.
    internal volatile bool Shown;

    internal static bool ValidAppId(string id) {
        return id != null && id.Length >= 17 && id.Length <= 20 && id.All(c => c >= '0' && c <= '9');
    }
    /// Environment first, then the launcher settings, then the built-in application.
    internal static string AppId(Preferences prefs) {
        var env = Environment.GetEnvironmentVariable("VELKIN_DISCORD_APP_ID");
        if (ValidAppId(env)) return env;
        if (prefs != null && ValidAppId(prefs.discordAppId)) return prefs.discordAppId;
        return ValidAppId(BuiltInAppId) ? BuiltInAppId : null;
    }
    internal static bool Allowed { get { return Product.Enabled("VELKIN_DISCORD"); } }
    internal static bool Active(Preferences prefs) { return prefs.discord && Allowed && AppId(prefs) != null; }

    internal static Presence Begin(Preferences prefs, Installation game, Locale locale) {
        var presence = new Presence();
        if (!Active(prefs)) return presence;
        string id = AppId(prefs);
        string activity = Activity(game.Title, prefs.discordGame, locale, DateTimeOffset.UtcNow.ToUnixTimeSeconds(),
            System.Diagnostics.Process.GetCurrentProcess().Id);
        presence.worker = new Thread(() => presence.Run(id, activity)) { IsBackground = true, Name = "Discord Rich Presence" };
        presence.worker.Start();
        return presence;
    }

    /// Discord rejects strings shorter than 2 or longer than 128 characters.
    static string Field(string s) {
        s = (s ?? "").Trim();
        if (s.Length > 128) s = s.Substring(0, 127) + "…";
        return s.Length < 2 ? s.PadRight(2) : s;
    }
    internal static string Activity(string title, bool showTitle, Locale locale, long start, int pid) {
        var activity = new Dictionary<string,object> {
            { "details", Field(showTitle && !String.IsNullOrWhiteSpace(title) ? title : locale.T("discordHidden")) },
            { "state", Field(locale.T("discordState", Product.Version)) },
            { "timestamps", new Dictionary<string,object> { { "start", start } } },
            { "assets", new Dictionary<string,object> { { "large_image", LargeImage }, { "large_text", Field("Velkin " + Product.Version) } } },
        };
        var command = new Dictionary<string,object> {
            { "cmd", "SET_ACTIVITY" },
            { "args", new Dictionary<string,object> { { "pid", pid }, { "activity", activity } } },
            { "nonce", Guid.NewGuid().ToString() },
        };
        return Files.Json.Serialize(command);
    }
    internal static string Handshake(string id) {
        return Files.Json.Serialize(new Dictionary<string,object> { { "v", 1 }, { "client_id", id } });
    }

    /// A frame is a little-endian opcode, a little-endian length, then UTF-8 JSON.
    internal static byte[] Frame(int op, string json) {
        var payload = Encoding.UTF8.GetBytes(json);
        var frame = new byte[8 + payload.Length];
        BitConverter.GetBytes(op).CopyTo(frame, 0);
        BitConverter.GetBytes(payload.Length).CopyTo(frame, 4);
        payload.CopyTo(frame, 8);
        return frame;
    }
    internal static void Write(Stream s, int op, string json) {
        var frame = Frame(op, json);
        s.Write(frame, 0, frame.Length);
        s.Flush();
    }
    static void Fill(Stream s, byte[] buffer) {
        for (int at = 0; at < buffer.Length;) {
            int n = s.Read(buffer, at, buffer.Length - at);
            if (n <= 0) throw new EndOfStreamException();
            at += n;
        }
    }
    internal static KeyValuePair<int,string> Read(Stream s) {
        var header = new byte[8];
        Fill(s, header);
        int op = BitConverter.ToInt32(header, 0), length = BitConverter.ToInt32(header, 4);
        if (length < 0 || length > MaxFrame) throw new InvalidDataException("Discord frame too large");
        var payload = new byte[length];
        Fill(s, payload);
        return new KeyValuePair<int,string>(op, Encoding.UTF8.GetString(payload));
    }

    const int Handshaking = 0, Message = 1, Close = 2, Ping = 3, Pong = 4;
    void Run(string id, string activity) {
        for (int i = 0; i < 10; i++) {
            // Overlapped I/O, so that closing the pipe from Dispose also ends a pending read.
            var stream = new NamedPipeClientStream(".", PipePrefix + i, PipeDirection.InOut, PipeOptions.Asynchronous);
            try { stream.Connect(150); }
            catch (Exception) { stream.Dispose(); continue; }
            lock (gate) {
                if (closed) { stream.Dispose(); return; }
                pipe = stream;
            }
            try {
                Send(stream, Handshaking, Handshake(id));
                var ready = Read(stream);
                // A refused Application ID closes with op 2: another pipe would refuse it too.
                if (ready.Key != Message) return;
                Send(stream, Message, activity);
                while (true) {
                    var frame = Read(stream);
                    if (frame.Key == Message && frame.Value.Contains("\"SET_ACTIVITY\"") && !frame.Value.Contains("\"ERROR\"")) Shown = true;
                    else if (frame.Key == Ping) Send(stream, Pong, frame.Value);
                    else if (frame.Key == Close) return;
                }
            } catch (Exception) {
                // Discord quit or the game ended; either way there is nothing to retry.
                return;
            } finally { Drop(stream); }
        }
    }
    void Send(Stream stream, int op, string json) {
        lock (gate) { if (!closed) Write(stream, op, json); }
    }
    void Drop(NamedPipeClientStream stream) {
        lock (gate) {
            if (pipe == stream) pipe = null;
            stream.Dispose();
        }
    }
    public void Dispose() {
        lock (gate) {
            if (closed) return;
            closed = true;
            if (pipe == null) return;
            // Clear the activity explicitly, then disconnect; Discord also clears it on disconnect.
            if (Shown) try {
                Write(pipe, Message, Files.Json.Serialize(new Dictionary<string,object> {
                    { "cmd", "SET_ACTIVITY" },
                    { "args", new Dictionary<string,object> { { "pid", System.Diagnostics.Process.GetCurrentProcess().Id }, { "activity", null } } },
                    { "nonce", Guid.NewGuid().ToString() } }));
            } catch (Exception) { }
            pipe.Dispose(); pipe = null;
        }
    }
}
}
