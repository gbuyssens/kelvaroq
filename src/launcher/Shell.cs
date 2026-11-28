using Microsoft.Win32;
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Runtime.InteropServices;
using System.Reflection;
using System.Windows.Markup;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Interop;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using System.Windows.Threading;
using Forms = System.Windows.Forms;

namespace Velkin {
internal sealed class Shell : Window {
    readonly string home, history;
    readonly bool demo, experimental;
    readonly Product.Receipt build;
    readonly BitmapImage mark = new BitmapImage();
    Preferences prefs;
    Hardware hardware;
    Locale locale;
    string page = "workspace";
    bool dark, working;
    Grid root;
    StackPanel body, nav;
    TextBlock status;
    ProgressBar progress;

    readonly ReleaseCheck updates;
    bool checking, showCheckResult;
    StackPanel updateNotice;
    Button checkUpdates;
    TextBlock checkResult;
    bool UpdatesEnabled { get { return Product.Enabled("VICTORY_LAUNCHER_UPDATES") && prefs.updates; } }

    internal int ExitStatus;
    readonly List<Button> navigation = new List<Button>();
    [DllImport("dwmapi.dll")] static extern int DwmSetWindowAttribute(IntPtr hwnd, int attr, ref int value, int size);

    internal Shell(string folder, Preferences preferences, bool preview, bool exp) {
        home = folder; history = Path.Combine(home,"snapshots"); prefs = preferences;
        demo = preview; experimental = exp; build = Product.Build(); locale = new Locale(prefs.language);
        if (Hardware.Enabled) hardware = demo ? Hardware.Sample() : Hardware.Detect();
        updates = !demo && Product.Enabled("VICTORY_LAUNCHER_UPDATES") ? ReleaseCheck.Load(home) : new ReleaseCheck();
        if (!demo) Loaded += delegate { CheckForUpdates(false); };
        var assembly = Assembly.GetExecutingAssembly();
        using (var stream = assembly.GetManifestResourceStream("controls.xaml"))
            Resources.MergedDictionaries.Add((ResourceDictionary)XamlReader.Load(stream));
        using (var stream = assembly.GetManifestResourceStream("mark.png")) {
            mark.BeginInit(); mark.CacheOption = BitmapCacheOption.OnLoad; mark.StreamSource = stream; mark.EndInit(); mark.Freeze();
        }
        Icon = mark;
        Title = "Velkin"; Width = Math.Min(1000,SystemParameters.WorkArea.Width); Height = Math.Min(730,SystemParameters.WorkArea.Height);
        MinWidth = Math.Min(760,Width); MinHeight = Math.Min(500,Height);
        FontFamily = new FontFamily("Segoe UI Variable Text, Segoe UI"); FontSize = 14;
        UseLayoutRounding = true; WindowStartupLocation = WindowStartupLocation.CenterScreen;
        dark = SystemDark(); AllowDrop = !demo && Product.Enabled("VICTORY_LAUNCHER_DROP");
        DragOver += (s,e) => { e.Effects = !working && e.Data.GetDataPresent(DataFormats.FileDrop) ? DragDropEffects.Copy : DragDropEffects.None; e.Handled = true; };
        Drop += (s,e) => Guard(delegate {
            if (working || demo) return;
            var paths = e.Data.GetData(DataFormats.FileDrop) as string[];
            if (paths == null || paths.Length != 1) throw new Problem("selectExe");
            Select(paths[0]); page = "workspace"; Draw();
        });
        Closing += (s,e) => e.Cancel = working;
        SourceInitialized += delegate { Chrome(); };
        Microsoft.Win32.SystemEvents.UserPreferenceChanged += ThemeChanged;
        Closed += delegate { Microsoft.Win32.SystemEvents.UserPreferenceChanged -= ThemeChanged; };
        Draw();
    }
    static bool SystemDark() {
        return Convert.ToInt32(Registry.GetValue(@"HKEY_CURRENT_USER\Software\Microsoft\Windows\CurrentVersion\Themes\Personalize","AppsUseLightTheme",1) ?? 1) == 0;
    }
    void Chrome() {
        int value = dark ? 1 : 0;
        var hwnd = new WindowInteropHelper(this).Handle;
        if (hwnd != IntPtr.Zero) DwmSetWindowAttribute(hwnd,20,ref value,4);
    }
    void ThemeChanged(object sender,UserPreferenceChangedEventArgs e) {
        Dispatcher.BeginInvoke(new Action(delegate {
            if (!working && dark != SystemDark()) { dark = SystemDark(); Chrome(); Draw(); }
        }));
    }
    string T(string key,params object[] args) { return locale.T(key,args); }
    Brush B(string light,string night) {
        return new SolidColorBrush((Color)ColorConverter.ConvertFromString(dark ? night : light));
    }
    Brush Ink { get { return B("#202925","#edf2ef"); } }
    Brush Muted { get { return B("#52635b","#b3c1b9"); } }
    Brush Accent { get { return B("#17634c","#8ad7b5"); } }
    Brush Panel { get { return B("#ffffff","#252c28"); } }
    Brush Edge { get { return B("#d7dfda","#414b44"); } }
    TextBlock Text(string s,double size,Brush color,bool bold = false) {
        return new TextBlock { Text = s, Foreground = color, FontSize = size,
            FontWeight = bold ? FontWeights.SemiBold : FontWeights.Normal, TextWrapping = TextWrapping.Wrap,
            VerticalAlignment = VerticalAlignment.Center };
    }
    void Guard(Action act) {
        try { act(); } catch (Exception ex) { MessageBox.Show(this,locale.Error(ex),Title,MessageBoxButton.OK,MessageBoxImage.Warning); }
    }
    Button Button(string caption,Action act,bool primary = false) {
        var button = new Button { Content = caption, MinHeight = 34, Padding = new Thickness(14,5,14,5),
            Margin = new Thickness(0,2,8,2), Background = primary ? Accent : Panel, Foreground = primary ? B("#ffffff","#11281c") : Ink,
            BorderBrush = primary ? Accent : Edge, BorderThickness = new Thickness(1), FontSize = 14,
            HorizontalContentAlignment = HorizontalAlignment.Center };
        var border = new FrameworkElementFactory(typeof(Border));
        border.Name = "outline";
        foreach (var pair in new[]{new[]{Border.BackgroundProperty,Control.BackgroundProperty},new[]{Border.BorderBrushProperty,Control.BorderBrushProperty},
                new[]{Border.BorderThicknessProperty,Control.BorderThicknessProperty},new[]{Border.PaddingProperty,Control.PaddingProperty}})
            border.SetValue(pair[0],new TemplateBindingExtension(pair[1]));
        border.SetValue(Border.CornerRadiusProperty,new CornerRadius(4));
        var content = new FrameworkElementFactory(typeof(ContentPresenter));
        content.SetValue(ContentPresenter.HorizontalAlignmentProperty,new TemplateBindingExtension(Control.HorizontalContentAlignmentProperty));
        content.SetValue(ContentPresenter.VerticalAlignmentProperty,VerticalAlignment.Center);
        border.AppendChild(content);
        var template = new ControlTemplate(typeof(Button)) { VisualTree = border };
        var hover = new Trigger { Property = UIElement.IsMouseOverProperty, Value = true };
        hover.Setters.Add(new Setter(Border.BorderBrushProperty,Accent,"outline")); template.Triggers.Add(hover);
        var focus = new Trigger { Property = UIElement.IsKeyboardFocusedProperty, Value = true };
        focus.Setters.Add(new Setter(Border.BorderBrushProperty,Ink,"outline")); focus.Setters.Add(new Setter(Border.BorderThicknessProperty,new Thickness(2),"outline")); template.Triggers.Add(focus);
        var disabled = new Trigger { Property = UIElement.IsEnabledProperty, Value = false };
        disabled.Setters.Add(new Setter(UIElement.OpacityProperty,0.5)); template.Triggers.Add(disabled);
        button.Template = template; button.Click += delegate { Guard(act); };
        return button;
    }
    void Draw() {
        navigation.Clear();
        Language = System.Windows.Markup.XmlLanguage.GetLanguage(locale.Culture.IetfLanguageTag);
        Resources["Ink"] = Ink; Resources["Edge"] = Edge; Resources["Panel"] = Panel;
        Resources["Accent"] = Accent; Resources["Selection"] = B("#dce8e1","#34433a");
        root = new Grid { Background = B("#f7f9f7","#1b211e") }; Content = root;
        root.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(207) });
        root.ColumnDefinitions.Add(new ColumnDefinition());
        var rail = new DockPanel { Background = B("#edf1ee","#202723"), Margin = new Thickness(0) };
        root.Children.Add(rail);
        var brand = new StackPanel { Orientation = Orientation.Horizontal, Margin = new Thickness(22,28,10,32) };
        brand.Children.Add(new Border { Width = 25, Height = 25, Background = Accent, OpacityMask = new ImageBrush(mark),
            Margin = new Thickness(0,0,9,0), VerticalAlignment = VerticalAlignment.Center });
        brand.Children.Add(Text("Velkin",23,Ink,true));
        DockPanel.SetDock(brand,Dock.Top); rail.Children.Add(brand);
        var foot = Text((demo ? T("demo") + "   ·   " : "") + "v" + Product.Version,12,Muted);
        foot.Margin = new Thickness(24,12,16,18); DockPanel.SetDock(foot,Dock.Bottom); rail.Children.Add(foot);
        nav = new StackPanel { Margin = new Thickness(10,0,10,0) }; rail.Children.Add(nav);
        string[] pages = {"workspace","saves","settings","about"}, glyphs = {"\uE80F","\uE81C","\uE713","\uE946"};
        for (int i = 0; i < pages.Length; i++) {
            string key = pages[i]; if (!demo && key == "saves" && !Product.Enabled("VICTORY_SAVE_HISTORY")) continue;
            int index = navigation.Count;
            var b = Button(T(key),delegate { page = key; Draw(); navigation[index].Focus(); });
            var row = new StackPanel { Orientation = Orientation.Horizontal };
            row.Children.Add(new TextBlock { Text = glyphs[i], FontFamily = new FontFamily("Segoe MDL2 Assets"), FontSize = 15, VerticalAlignment = VerticalAlignment.Center,
                Margin = new Thickness(0,0,12,0) }); row.Children.Add(Text(T(key == "saves" ? "saveNav" : key),14,Ink,page == key));
            b.Content = row; AutomationProperties.SetName(b,T(key)); b.Padding = new Thickness(12,8,4,8);
            b.HorizontalContentAlignment = HorizontalAlignment.Left; b.Margin = new Thickness(0,1,0,2);
            b.Background = page == key ? B("#dce8e1","#34433a") : Brushes.Transparent;
            b.BorderBrush = page == key ? Accent : Brushes.Transparent; b.BorderThickness = new Thickness(3,0,0,0);
            nav.Children.Add(b); navigation.Add(b);
        }
        var main = new DockPanel { Margin = new Thickness(30,24,30,16) }; Grid.SetColumn(main,1); root.Children.Add(main);
        var title = Text(T(page),30,Ink,true); title.Margin = new Thickness(0,0,0,18); DockPanel.SetDock(title,Dock.Top); main.Children.Add(title);
        checkUpdates = null; checkResult = null;
        updateNotice = new StackPanel(); DockPanel.SetDock(updateNotice,Dock.Top); main.Children.Add(updateNotice);
        var feedback = new StackPanel(); DockPanel.SetDock(feedback,Dock.Bottom); main.Children.Add(feedback);
        status = Text("",13,Muted); feedback.Children.Add(status);
        progress = new ProgressBar { Height = 4, Visibility = Visibility.Collapsed, Foreground = Accent }; feedback.Children.Add(progress);
        body = new StackPanel { Margin = new Thickness(0,0,6,0) };
        main.Children.Add(new ScrollViewer { Content = body, VerticalScrollBarVisibility = ScrollBarVisibility.Auto, HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled });
        switch (page) { case "workspace": Workspace(); break; case "settings": Settings(); break; case "saves": Saves(); break; case "about": About(); break; }
        UpdateFeedback();
    }
    StackPanel Card(string title) {
        if (title != null) { var header = Text(T(title),17,Ink,true); header.Margin = new Thickness(0,10,0,8); body.Children.Add(header); }
        var box = new StackPanel();
        body.Children.Add(new Border { Child = box, Background = Panel, BorderBrush = Edge, BorderThickness = new Thickness(1), CornerRadius = new CornerRadius(5), Padding = new Thickness(16,8,14,8) });
        return box;
    }
    void Row(StackPanel panel,string caption,string detail,UIElement action = null) {
        var dock = new DockPanel { Margin = new Thickness(0,7,0,7) };
        if (action != null) { DockPanel.SetDock(action,Dock.Right); dock.Children.Add(action); }
        var text = new StackPanel { VerticalAlignment = VerticalAlignment.Center, Margin = new Thickness(0,0,10,0) };
        text.Children.Add(Text(caption,14,Ink,true));
        if (!String.IsNullOrEmpty(detail)) text.Children.Add(Text(detail,13,Muted));
        dock.Children.Add(text); panel.Children.Add(dock);
    }
    WrapPanel Actions(StackPanel panel) { var w = new WrapPanel { Margin = new Thickness(0,6,0,4) }; panel.Children.Add(w); return w; }
    bool Confirm(string key) { return MessageBox.Show(this,T(key),Title,MessageBoxButton.YesNo,MessageBoxImage.Question,MessageBoxResult.No) == MessageBoxResult.Yes; }
    void Persist() { if (!demo) prefs.Write(home); }
    string BuildId { get { return build.patched_tree ?? "unknown"; } }
    string BuildText(string id) { return id == "unknown" ? T("unknown") : id.Substring(0,Math.Min(12,id.Length)); }
    void Select(string path) {
        var game = Installation.Select(path); Files.Separate(game.Root,prefs.ActiveData); Files.Separate(game.Root,prefs.ActiveCache);
        prefs.executable = game.Image; Persist();
    }
    internal void OpenArgument(string path) {
        Guard(delegate { Select(path); Draw(); Launch(); });
    }
    void Choose() {
        if (demo) return;
        var d = new OpenFileDialog { Filter = T("execFilter") + " (*.exe)|*.exe", CheckFileExists = true, Title = T("choose") };
        if (!String.IsNullOrEmpty(prefs.executable) && File.Exists(prefs.executable)) d.InitialDirectory = Path.GetDirectoryName(prefs.executable);
        if (d.ShowDialog(this) == true) { Select(d.FileName); Draw(); }
    }
    void Workspace() {
        var card = Card("installation");
        Row(card,T("exe"),demo ? @"D:\Library\Sample\bin\sample.exe" : prefs.executable ?? T("notSelected"),
            Button(T(demo || prefs.executable != null ? "change" : "choose"),Choose));
        card.Children.Add(Text(T("drop"),13,Muted));
        var actions = Actions(card);
        var launch = Button(T("launch"),Launch,true); actions.Children.Add(launch); launch.IsEnabled = !demo && prefs.executable != null;
        var options = Card("options");
        Row(options,T("monitor"),null,Text(T(prefs.monitor && Product.Enabled("VICTORY_LAUNCHER_MONITOR") ? "on" : "off"),14,Muted));
        Row(options,T("preload"),null,Text(T(prefs.preload && Product.PreloadAllowed ? "on" : "off"),14,Muted));
        Row(options,T("discord"),null,Text(T(Presence.Active(prefs) ? "on" : "off"),14,Muted));
        if (hardware != null) Detected();
    }

    string Known(string value) { return String.IsNullOrWhiteSpace(value) ? T("unknown") : value; }
    string Capability(bool? value) { return T(!value.HasValue ? "unknown" : value.Value ? "featureYes" : "featureNo"); }
    void Detected() {
        var card = Card("detected");
        var gpu = hardware.Preferred;
        string vram = gpu == null ? T("unknown") : Size(gpu.vram);
        string ram = hardware.ram.HasValue ? Size(hardware.ram.Value) : T("unknown");
        string summary = String.Join("  ·  ",new[]{Known(gpu == null ? null : gpu.name),T("videoMemory",vram),
            Known(hardware.Cpu),T("systemMemory",ram)});
        var details = new List<string>();
        if (gpu != null) details.Add(gpu.VendorName + " " + T("driver",Known(gpu.driver)));
        if (hardware.physical > 0) details.Add(T("cores",hardware.physical,hardware.logical));
        details.Add(T("windowsBuild",Known(hardware.Build)));
        Row(card,summary,String.Join("  ·  ",details));
        foreach (string warning in hardware.Warnings(locale))
            card.Children.Add(Text(warning,14,B("#894419","#ffcd93"),true));
    }
    async void Work(Func<Task> action,string message) {
        if (working) return;
        working = true; root.IsEnabled = false; status.Text = message;
        progress.Visibility = Visibility.Visible; progress.IsIndeterminate = true;
        try { await action(); Draw(); status.Text = T("done"); }
        catch (Exception ex) { Draw(); status.Text = locale.Error(ex); }
        finally { working = false; root.IsEnabled = true; progress.Visibility = Visibility.Collapsed; }
    }
    void Launch() {
        if (demo || working) return;
        var game = Installation.Select(prefs.executable); Files.Separate(game.Root,prefs.ActiveData); Files.Separate(game.Root,prefs.ActiveCache);
        var command = Product.Command(game,prefs,locale,experimental,dark,hardware);
        Work(() => Task.Run(delegate {
            bool snapshots = Product.Enabled("VICTORY_SAVE_HISTORY") && Product.Enabled("VICTORY_SAVE_SNAPSHOTS");
            string error = "";
            ExitStatus = Session.Run(prefs,history,BuildId,snapshots,delegate {
                using (var child = Process.Start(command))
                using (Presence.Begin(prefs,game,locale)) {
                    error = child.StandardError.ReadToEnd(); child.WaitForExit(); return child.ExitCode;
                }
            });
            if (ExitStatus != 0) throw new IOException(T("exitError",ExitStatus) + "\n" + error.Trim());
        }),T("running"));
    }
    string Size(long n) {
        var unit = n >= 1073741824 ? "GiB" : n >= 1048576 ? "MiB" : n >= 1024 ? "KiB" : "B";
        double divisor = unit == "GiB" ? 1073741824 : unit == "MiB" ? 1048576 : unit == "KiB" ? 1024 : 1;
        return (n / divisor).ToString("0.#",locale.Culture) + " " + T(unit == "B" ? "bytes" : unit.ToLowerInvariant());
    }
    void Settings() {
        if (hardware != null && ProfileRule.Enabled) ProfileSettings();
        if (demo || Product.Enabled("VICTORY_LAUNCHER_STORAGE")) { var storage = Card("storage"); StorageRow(storage,true); StorageRow(storage,false); }
        var options = Card("options");
        foreach (var key in new[]{"monitor","preload"}) {
            bool monitor = key == "monitor"; var check = new CheckBox { Content = T(key), Foreground = Ink, VerticalContentAlignment = VerticalAlignment.Center,
                IsChecked = monitor ? prefs.monitor && Product.Enabled("VICTORY_LAUNCHER_MONITOR") : prefs.preload && Product.PreloadAllowed,
                IsEnabled = monitor ? Product.Enabled("VICTORY_LAUNCHER_MONITOR") : Product.PreloadAllowed, Margin = new Thickness(0,5,0,5) };
            check.Click += delegate { Guard(delegate { if (monitor) prefs.monitor = check.IsChecked == true; else prefs.preload = check.IsChecked == true; Persist(); }); };
            options.Children.Add(check);
        }
        DiscordSettings();
        if (Product.Enabled("VICTORY_LAUNCHER_UPDATES")) UpdateControls(true);
        if (demo || Product.Enabled("VICTORY_LAUNCHER_I18N")) LanguageSettings();
        if (demo || Product.Enabled("VICTORY_SAVE_HISTORY")) { var retention = Card("retention"); Count(retention,true); Count(retention,false); }
    }
    void DiscordSettings() {
        var card = Card("discord");
        bool allowed = Presence.Allowed;
        var show = new CheckBox { Content = T("discordToggle"), Foreground = Ink, IsChecked = prefs.discord && allowed, IsEnabled = allowed,
            VerticalContentAlignment = VerticalAlignment.Center, Margin = new Thickness(0,5,0,5) };
        var game = new CheckBox { Content = T("discordGame"), Foreground = Ink, IsChecked = prefs.discordGame, IsEnabled = allowed && prefs.discord,
            VerticalContentAlignment = VerticalAlignment.Center, Margin = new Thickness(24,5,0,5) };
        show.Click += delegate { Guard(delegate { prefs.discord = show.IsChecked == true; game.IsEnabled = prefs.discord; Persist(); }); };
        game.Click += delegate { Guard(delegate { prefs.discordGame = game.IsChecked == true; Persist(); }); };
        card.Children.Add(show); card.Children.Add(game);
        var id = new TextBox { Width = 200, Text = prefs.discordAppId ?? "", IsEnabled = allowed, Padding = new Thickness(4,3,4,3),
            Background = Panel, Foreground = Ink, BorderBrush = Edge, VerticalContentAlignment = VerticalAlignment.Center };
        AutomationProperties.SetName(id,T("discordAppId"));
        Row(card,T("discordAppId"),T("discordAppIdHelp"),id);
        var note = Text("",13,Muted); card.Children.Add(note);
        Action explain = delegate {
            string typed = id.Text.Trim();
            bool bad = typed.Length > 0 && !Presence.ValidAppId(typed);
            note.Foreground = bad ? B("#894419","#ffcd93") : Muted;
            note.Text = bad ? T("discordBadId") : typed.Length == 0 && Presence.AppId(prefs) == null ? T("discordNoId") : "";
        };
        id.LostFocus += delegate { Guard(delegate {
            string typed = id.Text.Trim();
            if (typed.Length == 0 || Presence.ValidAppId(typed)) { prefs.discordAppId = typed.Length == 0 ? null : typed; Persist(); }
            explain();
        }); };
        id.TextChanged += delegate { explain(); };
        explain();
    }
    void ProfileSettings() {
        var card = Card("hardwareProfile");
        var mode = new ComboBox { Width = 145, ItemsSource = new[]{T("automatic"),T("custom")},
            SelectedIndex = prefs.customProfile ? 1 : 0 };
        AutomationProperties.SetName(mode,T("hardwareProfile"));
        Row(card,T("profileMode"),null,mode);
        mode.SelectionChanged += delegate { Guard(delegate {
            prefs.customProfile = mode.SelectedIndex == 1; Persist(); Draw();
        }); };
        var gpu = hardware.Preferred;
        if (gpu != null) card.Children.Add(Text(T("capabilities",Capability(gpu.barriers),Capability(gpu.upload_heap),
            gpu.wave_min == 0 ? T("unknown") : gpu.wave_min == gpu.wave_max ? gpu.wave_max.ToString() :
            gpu.wave_min + "–" + gpu.wave_max),13,Muted));
        if (gpu == null || !gpu.Compatible) card.Children.Add(Text(T("defaultsUsed"),14,Muted));
        foreach (var rule in ProfileRule.Rules) {
            var value = rule.Value(hardware,prefs);
            string inherited = Environment.GetEnvironmentVariable(rule.Key);
            if (!value.HasValue && inherited == null) continue;
            string label = T(rule.Label);
            if (inherited != null)
                Row(card,label,rule.Key,Text(T("environmentValue",inherited),14,Ink));
            else if (prefs.customProfile) {
                var combo = new ComboBox { Width = 86, ItemsSource = rule.Choices, SelectedItem = value.Value };
                AutomationProperties.SetName(combo,label); Row(card,label,rule.Key,combo);
                combo.SelectionChanged += delegate { Guard(delegate {
                    if (prefs.profileValues == null) prefs.profileValues = new Dictionary<string,int>();
                    prefs.profileValues[rule.Key] = (int)combo.SelectedItem; Persist();
                }); };
            } else Row(card,label,rule.Key,Text(value.Value.ToString(locale.Culture),14,Ink,true));
        }
    }
    void LanguageSettings() {
        var lang = Card("language"); var combo = new ComboBox { MinWidth = 220, Margin = new Thickness(0,4,0,4), HorizontalAlignment = HorizontalAlignment.Left };
        combo.Items.Add(T("system")); foreach (string s in Locale.Names) combo.Items.Add(s);
        combo.SelectedIndex = Array.IndexOf(Locale.Codes,prefs.language) + 1; AutomationProperties.SetName(combo,T("language")); lang.Children.Add(combo);
        if (Array.IndexOf(Locale.Codes,locale.Code) >= 2) lang.Children.Add(Text(T("review"),13,Muted));
        combo.SelectionChanged += delegate { Guard(delegate {
            prefs.language = combo.SelectedIndex == 0 ? "system" : Locale.Codes[combo.SelectedIndex-1]; Persist(); locale = new Locale(prefs.language); Draw();
        }); };
    }
    void Count(StackPanel card,bool recent) {
        var combo = new ComboBox { Width = 78, ItemsSource = Enumerable.Range(recent ? 1 : 0,recent ? 100 : 366), SelectedItem = recent ? prefs.recent : prefs.days };
        string key = recent ? "recent" : "days"; AutomationProperties.SetName(combo,T(key)); Row(card,T(key),null,combo);
        combo.SelectionChanged += delegate { Guard(delegate { if (recent) prefs.recent = (int)combo.SelectedItem; else prefs.days = (int)combo.SelectedItem; Persist(); }); };
    }
    async void StorageRow(StackPanel card,bool cache) {
        string path = cache ? prefs.cache : prefs.data;
        string shown = demo ? (cache ? @"D:\Velkin\cache" : @"D:\Velkin\data") : path;
        var size = Text("…",13,Muted); Row(card,T(cache ? "cache" : "data"),shown,size);
        var row = Actions(card);
        row.Children.Add(Button(T("move"),delegate { MoveStorage(cache); }));
        if (cache || Product.Enabled("VICTORY_SAVE_HISTORY")) row.Children.Add(Button(T(cache ? "clear" : "clearData"),delegate {
            if (demo || !Confirm(cache ? "confirmClear" : "confirmData")) return;
            Work(() => Task.Run(delegate {
                if (cache) Files.ClearShaders(path);
                else Archives.ClearData(prefs,history,BuildId);
            }),T("busy"));
        }));
        try { size.Text = Size(demo ? (cache ? 524288000 : 87040) : await Task.Run(() => Files.Size(path))); }
        catch (Exception ex) { size.Text = locale.Error(ex); }
    }
    void MoveStorage(bool cache) {
        if (demo) return;
        using (var picker = new Forms.FolderBrowserDialog { Description = T("chooseDestination") }) {
            if (picker.ShowDialog() != Forms.DialogResult.OK) return;
            string dest = Path.Combine(picker.SelectedPath,cache ? "Velkin-cache" : "Velkin-data");
            var original = prefs;
            var next = Files.Json.Deserialize<Preferences>(Files.Json.Serialize(prefs));
            if (cache) next.cache = dest; else next.data = dest;
            next.Validate(home);
            if (prefs.executable != null) Files.Separate(dest,Installation.Select(prefs.executable).Root);
            var reporter = new Progress<int>(v => { progress.IsIndeterminate = false; progress.Value = v; status.Text = T("move").TrimEnd('…') + "  " + v + " %"; });
            Work(async delegate {
                await Task.Run(() => Relocation.Move(cache ? prefs.cache : prefs.data,dest,((IProgress<int>)reporter).Report,useNew => { var selected = useNew ? next : original; selected.Write(home); prefs = selected; }));
            },T("busy"));
        }
    }
    void Saves() {
        if (Product.Enabled("VICTORY_SAVE_SNAPSHOTS")) body.Children.Add(Text(T("historyHint"),14,Muted));
        var card = Card(null); card.Margin = new Thickness(0,4,0,0);
        var list = new ListBox { MaxHeight = 225, MinHeight = 90, Background = Panel, BorderBrush = Edge };
        AutomationProperties.SetName(list,T("saves"));
        var itemStyle = new Style(typeof(ListBoxItem));
        itemStyle.Setters.Add(new Setter(Control.PaddingProperty,new Thickness(4,2,4,2)));
        var selected = new Trigger { Property = ListBoxItem.IsSelectedProperty, Value = true };
        selected.Setters.Add(new Setter(Control.BackgroundProperty,B("#dce8e1","#34433a")));
        itemStyle.Triggers.Add(selected);
        list.Resources[SystemColors.HighlightBrushKey] = B("#dce8e1","#34433a");
        list.Resources[SystemColors.InactiveSelectionHighlightBrushKey] = B("#dce8e1","#34433a");
        list.ItemContainerStyle = itemStyle;
        var entries = demo ? Samples() : Archives.History(history);
        foreach (var snap in entries) {
            var line = new StackPanel { Margin = new Thickness(6,5,6,5) };
            line.Children.Add(Text(snap.Time.ToLocalTime().ToString("g",locale.Culture) + "    ·    " + Size(snap.Info.files.Sum(f => f.size)),14,Ink,true));
            string reason = snap.Info.reason;
            line.Children.Add(Text((String.IsNullOrEmpty(snap.Info.label) ? T(new[]{"beforeLaunch","afterExit","beforeRestore","badArchive"}.Contains(reason) ? reason : "manual") : snap.Info.label) +
                "    ·    v" + snap.Info.launcher_version + "    ·    " + BuildText(snap.Info.engine_build),13,Muted));
            list.Items.Add(new ListBoxItem { Content = line, Tag = snap });
        }
        if (entries.Count > 0) { card.Children.Add(list); list.SelectedIndex = 0; } else card.Children.Add(Text(T("empty"),14,Muted));
        var actions = Actions(card);
        foreach (string key in new[]{"restore","export","verify","import"}) {
            var b = Button(T(key),delegate { if (!demo) SaveAction(key,(ListBoxItem)list.SelectedItem); });
            b.IsEnabled = key == "import" || entries.Count > 0; actions.Children.Add(b);
        }
        var create = Card("snapshot");
        var label = new TextBox { Background = Panel, Foreground = Ink, BorderBrush = Edge, CaretBrush = Ink, MaxLength = 120, MinWidth = 200, Padding = new Thickness(5), Margin = new Thickness(0,4,0,4) };
        AutomationProperties.SetName(label,T("label")); create.Children.Add(Text(T("label"),13,Muted)); create.Children.Add(label);
        var button = Button(T("snapshot"),delegate {
            if (demo) return; string value = label.Text.Trim();
            Work(() => Task.Run(delegate { Archives.Write(prefs.ActiveData,history,BuildId,"manual",value); Archives.Prune(history,prefs); }),T("busy"));
        }); Actions(create).Children.Add(button);
    }
    void SaveAction(string key,ListBoxItem item) {
        string file = item == null ? null : ((Snapshot)item.Tag).Path;
        string filter = T("zipFilter") + " (*.zip)|*.zip";
        if (key == "export") {
            var save = new SaveFileDialog { Filter = filter, DefaultExt = ".zip", FileName = "Velkin-" + DateTime.Today.ToString("yyyyMMdd") };
            if (save.ShowDialog(this) != true) return;
            foreach (var path in new[]{prefs.data,prefs.cache,home}) Files.Separate(save.FileName,path);
            Work(() => Task.Run(() => Archives.Export(file,save.FileName)),T("busy"));
        } else if (key == "verify") {
            Work(async delegate { await Task.Run(() => Archives.Inspect(file,null)); MessageBox.Show(this,T("verified"),Title); },T("busy"));
        } else {
            if (key == "import") {
                var open = new OpenFileDialog { Filter = filter, CheckFileExists = true };
                if (open.ShowDialog(this) != true) return; file = open.FileName;
            }
            if (Confirm("confirmRestore")) Work(() => Task.Run(() => Archives.Restore(file,prefs,history,BuildId)),T("busy"));
        }
    }
    void About() {
        var info = Card("installation");
        Row(info,T("productVersion"),Product.Version); Row(info,T("engineBuild"),BuildText(demo ? "63b902fe1720" : BuildId));
        Row(info,T("patch"),demo ? "0002" : build.patches == null || build.patches.Length == 0 ? T("unknown") : build.patches.Last().name.Split('-')[0]);
        Row(info,T("licence"),"GPL-3.0-or-later");
        var credits = Card("credits");
        Row(credits,T("publisher"),"Velkin contributors");
        Row(credits,"Kelvaroq","Stellarr Studio SAS, GPL-3.0-or-later");
        Row(credits,"VIctory","GPL-3.0-or-later");
        Row(credits,"VIctory-NVIDIA","GPL-3.0-or-later"); Row(credits,"victory-fork","GPL-3.0-or-later");
        if (Product.Enabled("VICTORY_LAUNCHER_UPDATES")) UpdateControls(false);
    }
    void UpdateControls(bool settings) {
        var card = Card("updates");
        if (settings) {
            var toggle = new CheckBox { Content = T("autoUpdates"), Foreground = Ink, IsChecked = prefs.updates, Margin = new Thickness(0,5,0,5) };
            toggle.Click += delegate { Guard(delegate {
                prefs.updates = toggle.IsChecked == true;
                Persist(); showCheckResult = false;
                if (prefs.updates) CheckForUpdates(false);
                UpdateFeedback();
            }); };
            card.Children.Add(toggle);
        }
        checkUpdates = Button(T("checkUpdates"),delegate { CheckForUpdates(true); });
        Actions(card).Children.Add(checkUpdates);
        checkResult = Text("",13,Muted); card.Children.Add(checkResult);
    }
    async void CheckForUpdates(bool manual) {
        if (!UpdatesEnabled || checking) return;
        showCheckResult = manual;
        try {
            if (!updates.Begin(UpdatesEnabled,DateTime.UtcNow,delegate { if (!demo) updates.Write(home); })) return;
            checking = true; UpdateFeedback();
            var latest = demo ? new Release { tag_name = "v0.3.0", body = T("sampleNotes") } : await Task.Run(() => ReleaseCheck.Fetch());
            if (latest != null) { updates.latest = latest; updates.succeeded = true; }
            if (!demo) updates.Write(home);
        } catch (Exception) {
            // An unavailable service or unwritable cache should never interrupt a launch.
        } finally { checking = false; UpdateFeedback(); }
    }
    void UpdateFeedback() {
        updateNotice.Children.Clear();
        var release = updates.latest;
        if (UpdatesEnabled && release != null && release.NewerThan(Product.Version)) {
            var notice = new StackPanel { Margin = new Thickness(0,0,0,12) };
            Row(notice,T("newRelease",release.tag_name),release.body,Button(T("openRelease"),delegate {
                if (!demo) Process.Start(new ProcessStartInfo(release.Page) { UseShellExecute = true });
            }));
            updateNotice.Children.Add(notice);
        }
        if (checkUpdates == null) return;
        checkUpdates.IsEnabled = UpdatesEnabled && !checking;
        string message = !UpdatesEnabled ? "" : checking ? T("checking") : !showCheckResult ? "" :
            !updates.succeeded ? T("updateOffline") : release != null && release.NewerThan(Product.Version) ? "" : T("upToDate");
        checkResult.Text = message;
        checkResult.Visibility = message.Length == 0 ? Visibility.Collapsed : Visibility.Visible;
    }
    List<Snapshot> Samples() {
        return Enumerable.Range(0,3).Select(n => new Snapshot { Info = new Manifest { created_at = new DateTime(2026,11,24,16-n,30,0,DateTimeKind.Utc).ToString("o"),
            reason = n == 1 ? "beforeLaunch" : "afterExit", launcher_version = Product.Version, engine_build = "63b902fe1720",
            files = new List<SavedFile> { new SavedFile { size = 87040 - n * 1024 } } } }).ToList();
    }
    internal void Capture(string directory) {
        foreach (var lang in new[]{"en","fr"}) foreach (var night in new[]{false,true}) {
            locale = new Locale(lang); prefs.language = lang; dark = night;
            string dest = Path.Combine(directory,lang,night ? "dark" : "light"); Directory.CreateDirectory(dest);
            var screens = new List<string> {"workspace","saves","settings","about","update"};
            if (hardware != null) screens.Add("unsupported");
            if (hardware != null && ProfileRule.Enabled) screens.Add("custom");
            foreach (var p in screens) {
                if (hardware != null) {
                    hardware = Hardware.Sample();
                    if (p == "unsupported") {
                        hardware.gpus[0].name = "Intel UHD Graphics"; hardware.gpus[0].vendor = 0x8086;
                        hardware.gpus[0].vram = 128L << 20; hardware.gpus[0].mesh = false;
                        hardware.gpus[0].uma = true; hardware.gpus[0].driver = null;
                        hardware.Cpu = "Intel Core i5"; hardware.physical = 4; hardware.logical = 8;
                        hardware.ram = 8L << 30;
                    }
                    prefs.customProfile = p == "custom";
                }
                updates.latest = p == "update" ? new Release { tag_name = "v0.3.0", body = T("sampleNotes") } : null;
                page = p == "update" || p == "unsupported" ? "workspace" : p == "custom" ? "settings" : p;
                Draw();
                // Capture full content, including settings below the fold on small displays.
                var size = new Size(1000,page == "settings" ? (ProfileRule.Enabled ? 1480 : 1020) : p == "about" ? 850 : 690);
                root.Measure(size); root.Arrange(new Rect(size)); root.UpdateLayout();
                var bmp = new RenderTargetBitmap((int)size.Width,(int)size.Height,96,96,PixelFormats.Pbgra32);
                bmp.Render(root); var png = new PngBitmapEncoder(); png.Frames.Add(BitmapFrame.Create(bmp));
                using (var stream = File.Create(Path.Combine(dest,p + ".png"))) png.Save(stream);
            }
        }
    }
}
}
