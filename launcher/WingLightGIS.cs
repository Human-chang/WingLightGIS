// WingLightGIS 啟動器 (launcher/WingLightGIS.cs)
//
// 取代原本的「啟動_GIS工程工具箱.bat」。由 launcher\build_launcher.bat 用 Windows 內建的
// .NET Framework 4 編譯器 (csc.exe) 編成專案根目錄的 WingLightGIS.exe，App 圖示嵌在 exe 裡。
//
// 做的事 (1～4 跟原本的 .bat 相同)：
// 1. 建立 workspace_temp、config 資料夾。
// 2. 沒有 runtime\python.exe 時，問要不要執行 setup_portable_env.bat 建立環境。
// 3. 只替主程式這個子行程設環境變數 (不動系統與使用者的環境變數)：清掉 PYTHONHOME (舊版 ArcMap 的
//    Python 2.7 會設它)、PYTHONPATH 指到專案資料夾、UTF-8 與 GDAL 檔名編碼、PATH 前面加上 runtime 的各資料夾。
// 4. 用 runtime\pythonw.exe main.py 開主程式，參數原樣傳過去。
//    第一個參數是 --debug (或 --console) 時改用 python.exe 並顯示主控台：從命令提示字元執行時用那個視窗，
//    否則開一個新的；主程式出錯結束時，新開的視窗等按 Enter 才關，讓人看得到錯誤訊息。
// 5. 螢火蟲啟動畫面 (規範 Splash 元件)：先啟動主程式、再畫啟動畫面，兩邊同時進行。
//    主程式的主視窗第一次畫好時會設一個有名字的事件 (ui/launcher_link.py)，啟動器看到就關掉；
//    主程式結束也關，最多等 15 秒。主程式還沒開出主視窗就以錯誤結束時，跳訊息說明怎麼查原因。
//    --debug 不顯示啟動畫面。
// 6. 已經有一份開著 (主程式建的具名 Mutex 存在，ui/single_instance.py) 時不顯示啟動畫面：
//    照樣啟動主程式，它把檔案交給開著的那一份就結束；啟動器等它結束、傳回它的結束代碼，
//    並允許開著的那一份把視窗叫到最前面。Mutex 名稱照程式資料夾算 (InstanceMutexName，跟 Python 端同一個算法)。
//    命令列的相對路徑先轉成完整路徑 (主程式的工作資料夾是程式資料夾，不是使用者所在的資料夾)。
//
// 這個檔在 WingLightGIS 實際使用並有測試；設計規範 kit/splash_launcher.cs 是同一份，改了要兩邊一起改。
// 給其他工具參考時：SplashForm 與 Native 兩段 (分層視窗、逐像素透明度、光暈明暗) 可以直接用；
// Main 裡的 runtime、環境變數、setup 詢問是 WingLightGIS 自己的，換成自己的程式怎麼啟動。

using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Drawing.Imaging;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Windows.Forms;

[assembly: AssemblyTitle("WingLightGIS")]
[assembly: AssemblyProduct("WingLightGIS")]
[assembly: AssemblyCompany("Human-chang")]

static class Launcher
{
    const string AppName = "WingLightGIS";
    // 主程式用這個環境變數找到「主視窗畫好了」的事件 (ui/launcher_link.py 的 READY_EVENT_ENV)
    const string ReadyEventEnv = "WINGLIGHTGIS_READY_EVENT";
    const int AttachParentProcess = -1;
    const int AsfwAny = -1;

    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool AttachConsole(int processId);

    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool AllocConsole();

    [DllImport("user32.dll")]
    static extern bool AllowSetForegroundWindow(int processId);

    [STAThread]
    static int Main(string[] args)
    {
        Application.EnableVisualStyles();
        string root = AppDomain.CurrentDomain.BaseDirectory;
        string runtimeDir = Path.Combine(root, "runtime");
        string python = Path.Combine(runtimeDir, "python.exe");
        string pythonw = Path.Combine(runtimeDir, "pythonw.exe");
        string mainScript = Path.Combine(root, "main.py");
        bool debug = args.Length > 0 && (args[0] == "--debug" || args[0] == "--console");

        try
        {
            Directory.CreateDirectory(Path.Combine(root, "workspace_temp"));
            Directory.CreateDirectory(Path.Combine(root, "config"));
        }
        catch (Exception ex)
        {
            ShowError("沒辦法在程式資料夾裡建立 workspace_temp 或 config 資料夾。\n" +
                      "程式資料夾可能是唯讀的 (例如放在 C:\\Program Files)，請把整個資料夾搬到可以寫入的位置。\n\n" + ex.Message);
            return 1;
        }

        if (!File.Exists(python) && !OfferSetup(root, python))
        {
            return 1;
        }

        var startInfo = new ProcessStartInfo();
        startInfo.FileName = debug || !File.Exists(pythonw) ? python : pythonw;
        startInfo.Arguments = BuildArguments(mainScript, FullPathArguments(args));
        startInfo.WorkingDirectory = root;
        startInfo.UseShellExecute = false;
        SetChildEnvironment(startInfo, root, runtimeDir);

        if (debug)
        {
            return RunWithConsole(startInfo);
        }
        if (InstanceRunning(root))
        {
            return HandOffToRunningInstance(startInfo);
        }
        return RunWithSplash(startInfo, root);
    }

    // 選項 (- 開頭) 與空字串原樣；其他當成路徑轉成完整路徑，轉不了 (例如含 ") 也原樣
    static string[] FullPathArguments(string[] args)
    {
        var result = new string[args.Length];
        for (int i = 0; i < args.Length; i++)
        {
            result[i] = args[i];
            if (args[i].Length == 0 || args[i].StartsWith("-"))
            {
                continue;
            }
            try
            {
                result[i] = Path.GetFullPath(args[i]);
            }
            catch (Exception)
            {
            }
        }
        return result;
    }

    // ui/single_instance.py 的 instance_names：程式資料夾去掉結尾斜線、轉小寫，UTF-8 的 SHA-1 前 12 碼
    static string InstanceMutexName(string root)
    {
        string folder = root.TrimEnd('\\', '/').ToLowerInvariant();
        using (var sha1 = System.Security.Cryptography.SHA1.Create())
        {
            byte[] hash = sha1.ComputeHash(Encoding.UTF8.GetBytes(folder));
            var digest = new StringBuilder();
            for (int i = 0; i < 6; i++)
            {
                digest.Append(hash[i].ToString("x2"));
            }
            return @"Local\" + AppName + "-" + digest;
        }
    }

    static bool InstanceRunning(string root)
    {
        try
        {
            Mutex existing;
            if (!Mutex.TryOpenExisting(InstanceMutexName(root), out existing))
            {
                return false;
            }
            existing.Dispose();
            return true;
        }
        catch (UnauthorizedAccessException)
        {
            // 有這個 Mutex 但打不開，也是有一份在跑
            return true;
        }
    }

    static int HandOffToRunningInstance(ProcessStartInfo startInfo)
    {
        // 啟動器是使用者點開的，開著的那一份要靠這個才能跳到最前面 (不知道它是哪個行程，所以給全部)
        AllowSetForegroundWindow(AsfwAny);
        try
        {
            using (Process child = Process.Start(startInfo))
            {
                // 主程式自己會等開著的那一份最多 15 秒，交不出去時自己跳訊息
                child.WaitForExit();
                return child.ExitCode;
            }
        }
        catch (Exception ex)
        {
            ShowError("沒辦法啟動主程式：\n" + startInfo.FileName + "\n\n" + ex.Message +
                      "\n\n可以重新執行 setup_portable_env.bat 修復執行環境。");
            return 1;
        }
    }

    // 只影響主程式這個子行程，跟原本 .bat 的 setlocal 區段相同
    static void SetChildEnvironment(ProcessStartInfo startInfo, string root, string runtimeDir)
    {
        var env = startInfo.EnvironmentVariables;
        env.Remove("PYTHONHOME");
        env.Remove(ReadyEventEnv);
        env["PYTHONPATH"] = root;
        env["PYTHONIOENCODING"] = "utf-8";
        env["PYTHONUTF8"] = "1";
        // GDAL 檔名與壓縮檔編碼；GDAL_DATA、PROJ_LIB 由程式自己設 (放在中文資料夾時絕對路徑無效)
        env["GDAL_FILENAME_IS_UTF8"] = "YES";
        env["CPL_ZIP_ENCODING"] = "CP950";
        string[] front =
        {
            runtimeDir,
            Path.Combine(runtimeDir, @"Library\bin"),
            Path.Combine(runtimeDir, @"Library\usr\bin"),
            Path.Combine(runtimeDir, "Scripts"),
        };
        string oldPath = env["PATH"] ?? "";
        env["PATH"] = string.Join(";", front) + (oldPath.Length > 0 ? ";" + oldPath : "");
    }

    static int RunWithSplash(ProcessStartInfo startInfo, string root)
    {
        string eventName = @"Local\WingLightGIS-ready-" + Process.GetCurrentProcess().Id;
        using (var ready = new EventWaitHandle(false, EventResetMode.ManualReset, eventName))
        {
            startInfo.EnvironmentVariables[ReadyEventEnv] = eventName;
            Process child;
            try
            {
                child = Process.Start(startInfo);
            }
            catch (Exception ex)
            {
                ShowError("沒辦法啟動主程式：\n" + startInfo.FileName + "\n\n" + ex.Message +
                          "\n\n可以重新執行 setup_portable_env.bat 修復執行環境。");
                return 1;
            }
            using (child)
            {
                // 啟動器是使用者點開的，把「可以跳到最前面」的權利交給主程式
                AllowSetForegroundWindow(child.Id);
                // 規範：先啟動核心再畫啟動畫面。少了圖檔就不顯示啟動畫面，主程式照常開
                SplashForm splash = SplashForm.TryCreate(root);
                if (splash == null)
                {
                    return 0;
                }
                using (splash)
                {
                    splash.ShouldClose = () => ready.WaitOne(0) || child.HasExited;
                    Application.Run(splash);
                }
                if (!ready.WaitOne(0) && child.HasExited && child.ExitCode != 0)
                {
                    ShowError(AppName + " 沒有開起來 (結束代碼 " + child.ExitCode + ")。\n\n" +
                              "可以在命令提示字元執行「WingLightGIS.exe --debug」看錯誤訊息，" +
                              "或重新執行 setup_portable_env.bat 修復執行環境。");
                    return 1;
                }
                return 0;
            }
        }
    }

    static int RunWithConsole(ProcessStartInfo startInfo)
    {
        // 從命令提示字元執行時沿用那個視窗；點兩下捷徑時自己開一個
        bool ownConsole = !AttachConsole(AttachParentProcess);
        if (ownConsole)
        {
            AllocConsole();
        }
        int exitCode;
        try
        {
            using (Process process = Process.Start(startInfo))
            {
                process.WaitForExit();
                exitCode = process.ExitCode;
            }
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine("沒辦法啟動主程式：" + startInfo.FileName + "\n" + ex.Message);
            exitCode = 1;
        }
        if (ownConsole && exitCode != 0)
        {
            Console.WriteLine();
            Console.WriteLine("主程式結束，結束代碼 " + exitCode + "。按 Enter 關閉這個視窗。");
            Console.ReadLine();
        }
        return exitCode;
    }

    // 沒有 runtime 時：問要不要執行 setup_portable_env.bat，執行完 runtime 在了就回傳 true
    static bool OfferSetup(string root, string python)
    {
        string setup = Path.Combine(root, "setup_portable_env.bat");
        if (!File.Exists(setup))
        {
            ShowError("找不到執行環境 (runtime 資料夾)，也找不到建立環境用的 setup_portable_env.bat。\n請重新下載完整的 " + AppName + "。");
            return false;
        }
        DialogResult answer = MessageBox.Show(
            "還沒有建立 " + AppName + " 的執行環境 (runtime 資料夾)。\n\n" +
            "要現在執行 setup_portable_env.bat 建立嗎？它會下載需要的套件，要有網路連線，會花一些時間。",
            AppName, MessageBoxButtons.YesNo, MessageBoxIcon.Information);
        if (answer != DialogResult.Yes)
        {
            return false;
        }
        try
        {
            var setupInfo = new ProcessStartInfo(setup);
            setupInfo.WorkingDirectory = root;
            setupInfo.UseShellExecute = true;
            using (Process process = Process.Start(setupInfo))
            {
                process.WaitForExit();
            }
        }
        catch (Exception ex)
        {
            ShowError("沒辦法執行 setup_portable_env.bat：\n" + ex.Message);
            return false;
        }
        if (!File.Exists(python))
        {
            ShowError("執行環境沒有建立完成，請看 setup_portable_env.bat 視窗裡的訊息，處理後再開一次 " + AppName + "。");
            return false;
        }
        return true;
    }

    static string BuildArguments(string mainScript, string[] args)
    {
        var parts = new List<string>();
        parts.Add(Quote(mainScript));
        foreach (string arg in args)
        {
            parts.Add(Quote(arg));
        }
        return string.Join(" ", parts);
    }

    // 照 Windows 命令列的拆法 (CommandLineToArgvW) 加引號，讓 Python 收到的參數跟這裡一字不差
    static string Quote(string arg)
    {
        if (arg.Length > 0 && arg.IndexOfAny(new[] { ' ', '\t', '"' }) < 0)
        {
            return arg;
        }
        var sb = new StringBuilder("\"");
        int backslashes = 0;
        foreach (char c in arg)
        {
            if (c == '\\')
            {
                backslashes++;
                continue;
            }
            if (c == '"')
            {
                sb.Append('\\', backslashes * 2 + 1);
            }
            else
            {
                sb.Append('\\', backslashes);
            }
            backslashes = 0;
            sb.Append(c);
        }
        sb.Append('\\', backslashes * 2);
        sb.Append('"');
        return sb.ToString();
    }

    static void ShowError(string message)
    {
        MessageBox.Show(message, AppName, MessageBoxButtons.OK, MessageBoxIcon.Error);
    }
}

// 螢火蟲啟動畫面 (規範 Splash 元件，數值照規範 tokens.json 與 Splash 預覽)：
// 420×320、splash-bg 夜空底、radius-xl 圓角；中間 220 px 螢火蟲；下面產品名、「啟動中」與三個 6 px glow 圓點。
// 圖都由 tools/make_splash.py 先畫好 (assets/splash/)：螢火蟲亮、暗兩張，以及兩段字
// (不在這裡用 GDI+ 畫字：第一次建立字型會列舉系統所有字型，字型多的電腦要 0.5 秒)。
// 動態：尾巴光暈 2.2 秒由暗到亮再到暗 (暗圖上疊亮圖，改亮圖的透明度)；三個圓點 1.2 秒一輪、各差 0.4 秒。
// Windows 關掉動畫效果時不閃，停在最亮。無邊框、最上層、不搶焦點、不出現在工作列；放在滑鼠所在螢幕的正中央。
sealed class SplashForm : Form
{
    public const int MaxWaitMs = 15000;
    static readonly Color SplashBg = Color.FromArgb(0x15, 0x1A, 0x2E);
    static readonly Color Glow = Color.FromArgb(0xFF, 0xC8, 0x3D);
    const float BaseWidth = 420, BaseHeight = 320, Radius = 12, FireflySize = 220, Gap = 6, DotSize = 6;
    const float TitleLine = 16 * 1.5f, HintLine = 12 * 1.5f;
    const double GlowPeriod = 2.2, DotPeriod = 1.2, DotStep = 0.4, DotMin = 0.25;
    // 光暈與圓點都是慢慢的明暗，每秒 25 格就夠順，少佔一點主程式啟動時要用的 CPU
    const int FrameMs = 40;

    public Func<bool> ShouldClose;

    readonly float scale;
    readonly Bitmap bright, dim, title, hint, canvas;
    readonly Rectangle fireflyRect;
    readonly Point titlePos, hintPos;
    readonly float dotsX, dotsY;
    readonly bool animate;
    readonly Stopwatch clock = Stopwatch.StartNew();
    readonly System.Windows.Forms.Timer timer = new System.Windows.Forms.Timer();
    readonly ColorMatrix fade = new ColorMatrix();
    readonly ImageAttributes fadeAttributes = new ImageAttributes();

    public static SplashForm TryCreate(string root)
    {
        string dir = Path.Combine(root, @"assets\splash");
        foreach (string name in new[] { "firefly_bright", "firefly_dim", "title_1x", "title_2x", "hint_1x", "hint_2x" })
        {
            if (!File.Exists(Path.Combine(dir, name + ".png")))
            {
                return null;
            }
        }
        try
        {
            return new SplashForm(dir);
        }
        catch (Exception)
        {
            return null;
        }
    }

    SplashForm(string dir)
    {
        Point cursor = Cursor.Position;
        scale = MonitorScale(cursor);
        int width = (int)Math.Round(BaseWidth * scale);
        int height = (int)Math.Round(BaseHeight * scale);
        Rectangle area = Screen.FromPoint(cursor).WorkingArea;

        Text = "WingLightGIS 啟動中";
        FormBorderStyle = FormBorderStyle.None;
        ShowInTaskbar = false;
        StartPosition = FormStartPosition.Manual;
        Bounds = new Rectangle(area.Left + (area.Width - width) / 2, area.Top + (area.Height - height) / 2, width, height);

        int fireflyPx = (int)Math.Round(FireflySize * scale);
        bright = LoadScaled(Path.Combine(dir, "firefly_bright.png"), scale / 2);
        dim = LoadScaled(Path.Combine(dir, "firefly_dim.png"), scale / 2);
        // 字圖：100% 縮放用 1x，其他用 2x 縮小 (放大會糊)
        int textScale = scale <= 1.001f ? 1 : 2;
        title = LoadScaled(Path.Combine(dir, "title_" + textScale + "x.png"), scale / textScale);
        hint = LoadScaled(Path.Combine(dir, "hint_" + textScale + "x.png"), scale / textScale);
        canvas = new Bitmap(width, height, PixelFormat.Format32bppArgb);

        // 版面照規範預覽：直向置中，螢火蟲、產品名、「啟動中」之間各隔 6 px，文字行高 1.5 倍
        float top = (BaseHeight - (FireflySize + Gap + TitleLine + Gap + HintLine)) / 2;
        fireflyRect = new Rectangle((width - fireflyPx) / 2, (int)Math.Round(top * scale), fireflyPx, fireflyPx);
        float titleTop = (top + FireflySize + Gap) * scale;
        titlePos = new Point((width - title.Width) / 2, (int)Math.Round(titleTop + (TitleLine * scale - title.Height) / 2));
        float hintTop = (top + FireflySize + Gap + TitleLine + Gap) * scale;
        float rowWidth = hint.Width + 3 * (Gap + DotSize) * scale;
        hintPos = new Point((int)Math.Round((width - rowWidth) / 2), (int)Math.Round(hintTop + (HintLine * scale - hint.Height) / 2));
        dotsX = hintPos.X + hint.Width;
        dotsY = hintTop + (HintLine - DotSize) * scale / 2;

        animate = ClientAreaAnimationEnabled();
        timer.Interval = FrameMs;
        timer.Tick += OnTick;
    }

    protected override CreateParams CreateParams
    {
        get
        {
            const int WS_EX_TOPMOST = 0x8, WS_EX_TOOLWINDOW = 0x80, WS_EX_LAYERED = 0x80000, WS_EX_NOACTIVATE = 0x8000000;
            CreateParams cp = base.CreateParams;
            cp.ExStyle |= WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_LAYERED | WS_EX_NOACTIVATE;
            return cp;
        }
    }

    protected override bool ShowWithoutActivation
    {
        get { return true; }
    }

    protected override void OnLoad(EventArgs e)
    {
        base.OnLoad(e);
        Render();
        timer.Start();
    }

    void OnTick(object sender, EventArgs e)
    {
        if ((ShouldClose != null && ShouldClose()) || clock.ElapsedMilliseconds > MaxWaitMs)
        {
            timer.Stop();
            Close();
            return;
        }
        if (animate)
        {
            Render();
        }
    }

    void Render()
    {
        double t = clock.Elapsed.TotalSeconds;
        // 亮圖的透明度 0 → 1 → 0，光暈的透明度就在 0.35 與 1 之間 (兩張只差在光暈)
        float brightAlpha = animate ? (float)((1 - Math.Cos(2 * Math.PI * t / GlowPeriod)) / 2) : 1f;
        using (Graphics g = Graphics.FromImage(canvas))
        {
            g.Clear(Color.Transparent);
            g.SmoothingMode = SmoothingMode.AntiAlias;
            g.PixelOffsetMode = PixelOffsetMode.HighQuality;
            using (GraphicsPath shape = RoundedRect(new RectangleF(0, 0, canvas.Width, canvas.Height), Radius * scale))
            using (var bg = new SolidBrush(SplashBg))
            {
                g.FillPath(bg, shape);
            }
            g.DrawImage(dim, fireflyRect);
            if (brightAlpha > 0.004f)
            {
                fade.Matrix33 = brightAlpha;
                fadeAttributes.SetColorMatrix(fade);
                g.DrawImage(bright, fireflyRect, 0, 0, bright.Width, bright.Height, GraphicsUnit.Pixel, fadeAttributes);
            }
            g.DrawImage(title, new Rectangle(titlePos, title.Size));
            g.DrawImage(hint, new Rectangle(hintPos, hint.Size));
            float dot = DotSize * scale;
            for (int i = 0; i < 3; i++)
            {
                double phase = (((t - i * DotStep) % DotPeriod) + DotPeriod) % DotPeriod / DotPeriod;
                double alpha = animate ? DotMin + (1 - DotMin) * (1 - Math.Cos(2 * Math.PI * phase)) / 2 : 1;
                float x = dotsX + (Gap * (i + 1) + DotSize * i) * scale;
                using (var dotBrush = new SolidBrush(Color.FromArgb((int)Math.Round(alpha * 255), Glow)))
                {
                    g.FillEllipse(dotBrush, x, dotsY, dot, dot);
                }
            }
        }
        PushToScreen();
    }

    // 分層視窗：每個像素自帶透明度，圓角邊緣才平滑
    void PushToScreen()
    {
        IntPtr screenDc = Native.GetDC(IntPtr.Zero);
        IntPtr memDc = Native.CreateCompatibleDC(screenDc);
        IntPtr bitmap = canvas.GetHbitmap(Color.FromArgb(0));
        IntPtr old = Native.SelectObject(memDc, bitmap);
        try
        {
            var size = new Native.SIZE { cx = canvas.Width, cy = canvas.Height };
            var source = new Native.POINT { x = 0, y = 0 };
            var position = new Native.POINT { x = Left, y = Top };
            var blend = new Native.BLENDFUNCTION { BlendOp = 0, BlendFlags = 0, SourceConstantAlpha = 255, AlphaFormat = 1 };
            Native.UpdateLayeredWindow(Handle, screenDc, ref position, ref size, memDc, ref source, 0, ref blend, 2);
        }
        finally
        {
            Native.SelectObject(memDc, old);
            Native.DeleteObject(bitmap);
            Native.DeleteDC(memDc);
            Native.ReleaseDC(IntPtr.Zero, screenDc);
        }
    }

    protected override void Dispose(bool disposing)
    {
        if (disposing)
        {
            timer.Dispose();
            bright.Dispose();
            dim.Dispose();
            title.Dispose();
            hint.Dispose();
            canvas.Dispose();
            fadeAttributes.Dispose();
        }
        base.Dispose(disposing);
    }

    // 讀 PNG 並縮放 factor 倍成畫面上的大小 (先讀進記憶體，不鎖住檔案)
    static Bitmap LoadScaled(string path, float factor)
    {
        using (var stream = new MemoryStream(File.ReadAllBytes(path)))
        using (Image source = Image.FromStream(stream))
        {
            int w = Math.Max(1, (int)Math.Round(source.Width * factor));
            int h = Math.Max(1, (int)Math.Round(source.Height * factor));
            var scaled = new Bitmap(w, h, PixelFormat.Format32bppPArgb);
            using (Graphics g = Graphics.FromImage(scaled))
            {
                g.InterpolationMode = InterpolationMode.HighQualityBicubic;
                g.PixelOffsetMode = PixelOffsetMode.HighQuality;
                g.CompositingQuality = CompositingQuality.HighQuality;
                g.DrawImage(source, new Rectangle(0, 0, w, h));
            }
            return scaled;
        }
    }

    static GraphicsPath RoundedRect(RectangleF r, float radius)
    {
        float d = radius * 2;
        var path = new GraphicsPath();
        path.AddArc(r.Left, r.Top, d, d, 180, 90);
        path.AddArc(r.Right - d, r.Top, d, d, 270, 90);
        path.AddArc(r.Right - d, r.Bottom - d, d, d, 0, 90);
        path.AddArc(r.Left, r.Bottom - d, d, d, 90, 90);
        path.CloseFigure();
        return path;
    }

    static float MonitorScale(Point point)
    {
        try
        {
            IntPtr monitor = Native.MonitorFromPoint(new Native.POINT { x = point.X, y = point.Y }, 2);
            uint dpiX, dpiY;
            if (Native.GetDpiForMonitor(monitor, 0, out dpiX, out dpiY) == 0 && dpiX > 0)
            {
                return dpiX / 96f;
            }
        }
        catch (Exception)
        {
        }
        return 1f;
    }

    // 「顯示 Windows 中的動畫」關掉時 (SPI_GETCLIENTAREAANIMATION) 不閃
    static bool ClientAreaAnimationEnabled()
    {
        int enabled = 1;
        return !Native.SystemParametersInfo(0x1042, 0, ref enabled, 0) || enabled != 0;
    }
}

static class Native
{
    [StructLayout(LayoutKind.Sequential)]
    public struct POINT { public int x; public int y; }

    [StructLayout(LayoutKind.Sequential)]
    public struct SIZE { public int cx; public int cy; }

    [StructLayout(LayoutKind.Sequential, Pack = 1)]
    public struct BLENDFUNCTION { public byte BlendOp; public byte BlendFlags; public byte SourceConstantAlpha; public byte AlphaFormat; }

    [DllImport("user32.dll", SetLastError = true)]
    public static extern bool UpdateLayeredWindow(IntPtr hwnd, IntPtr hdcDst, ref POINT pptDst, ref SIZE psize,
        IntPtr hdcSrc, ref POINT pprSrc, int crKey, ref BLENDFUNCTION pblend, int dwFlags);

    [DllImport("user32.dll")]
    public static extern IntPtr GetDC(IntPtr hwnd);

    [DllImport("user32.dll")]
    public static extern int ReleaseDC(IntPtr hwnd, IntPtr hdc);

    [DllImport("gdi32.dll")]
    public static extern IntPtr CreateCompatibleDC(IntPtr hdc);

    [DllImport("gdi32.dll")]
    public static extern bool DeleteDC(IntPtr hdc);

    [DllImport("gdi32.dll")]
    public static extern IntPtr SelectObject(IntPtr hdc, IntPtr obj);

    [DllImport("gdi32.dll")]
    public static extern bool DeleteObject(IntPtr obj);

    [DllImport("user32.dll")]
    public static extern IntPtr MonitorFromPoint(POINT pt, uint flags);

    [DllImport("shcore.dll")]
    public static extern int GetDpiForMonitor(IntPtr monitor, int dpiType, out uint dpiX, out uint dpiY);

    [DllImport("user32.dll")]
    public static extern bool SystemParametersInfo(uint action, uint param, ref int value, uint winIni);
}
