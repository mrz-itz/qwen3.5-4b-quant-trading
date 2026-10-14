using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Windows.Forms;

[assembly: AssemblyTitle("大模型量化交易 · 模拟盘控制台")]
[assembly: AssemblyDescription("基于大模型的多模态量化交易与模拟盘监控系统")]
[assembly: AssemblyCompany("Antigravity Quant")]
[assembly: AssemblyProduct("大模型量化交易")]
[assembly: AssemblyCopyright("Copyright (C) 2026")]
[assembly: AssemblyVersion("2.0.0.0")]
[assembly: AssemblyFileVersion("2.0.0.0")]

namespace QuantTradeApp
{
    static class Program
    {
        private const string AppTitle = "大模型量化交易";

        [STAThread]
        static void Main(string[] args)
        {
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);

            string baseDir = AppDomain.CurrentDomain.BaseDirectory.TrimEnd('\\', '/');

            // 1. 检查是否请求创建桌面快捷方式
            if (args != null && args.Length > 0)
            {
                for (int i = 0; i < args.Length; i++)
                {
                    if (args[i] == "--create-shortcut" || args[i] == "-s" || args[i] == "/shortcut")
                    {
                        CreateDesktopShortcut(baseDir);
                        MessageBox.Show(
                            "桌面快捷方式「大模型量化交易」已成功创建！",
                            AppTitle,
                            MessageBoxButtons.OK,
                            MessageBoxIcon.Information
                        );
                        return;
                    }
                }
            }

            // 2. 自动检查桌面快捷方式，若不存在则静默创建，方便日常使用
            EnsureDesktopShortcut(baseDir);

            // 3. 寻找 Python 解释器 (优先使用虚拟环境中的 pythonw.exe，彻底无黑框)
            string pythonwPath = FindPythonw(baseDir);
            if (string.IsNullOrEmpty(pythonwPath) || !File.Exists(pythonwPath))
            {
                MessageBox.Show(
                    "未找到 Python 虚拟运行环境 (.venv)。\n\n" +
                    "请确认已在项目目录配置虚拟环境并安装依赖：\n" +
                    "1. 进入项目目录\n" +
                    "2. 执行: python -m venv .venv\n" +
                    "3. 执行: .venv\\Scripts\\pip install -r requirements.txt\n\n" +
                    "项目目录: " + baseDir,
                    AppTitle + " - 运行环境未就绪",
                    MessageBoxButtons.OK,
                    MessageBoxIcon.Warning
                );
                return;
            }

            // 4. 寻找启动入口脚本 app_platform.py
            string scriptPath = Path.Combine(baseDir, "app_platform.py");
            if (!File.Exists(scriptPath))
            {
                MessageBox.Show(
                    "未找到主程序脚本 app_platform.py。\n\n项目目录: " + baseDir,
                    AppTitle + " - 文件缺失",
                    MessageBoxButtons.OK,
                    MessageBoxIcon.Error
                );
                return;
            }

            // 5. 组装参数并无窗口启动
            try
            {
                string extraArgs = (args != null && args.Length > 0) ? " " + string.Join(" ", args) : "";
                ProcessStartInfo psi = new ProcessStartInfo();
                psi.FileName = pythonwPath;
                psi.Arguments = string.Format("\"{0}\"{1}", scriptPath, extraArgs);
                psi.WorkingDirectory = baseDir;
                psi.UseShellExecute = false;
                psi.CreateNoWindow = true;
                psi.WindowStyle = ProcessWindowStyle.Hidden;

                Process p = Process.Start(psi);
                if (p == null)
                {
                    MessageBox.Show(
                        "无法启动后台进程，请检查权限或杀毒软件拦截。",
                        AppTitle,
                        MessageBoxButtons.OK,
                        MessageBoxIcon.Error
                    );
                }
            }
            catch (Exception ex)
            {
                MessageBox.Show(
                    "启动应用程序时发生异常:\n" + ex.Message,
                    AppTitle + " - 启动异常",
                    MessageBoxButtons.OK,
                    MessageBoxIcon.Error
                );
            }
        }

        private static string FindPythonw(string baseDir)
        {
            string[] candidates = new string[]
            {
                Path.Combine(baseDir, @".venv\Scripts\pythonw.exe"),
                Path.Combine(baseDir, @".venv\pythonw.exe"),
                Path.Combine(baseDir, @"venv\Scripts\pythonw.exe"),
                Path.Combine(baseDir, @"venv\pythonw.exe"),
                @"D:\Anaconda\pythonw.exe",
                @"C:\Anaconda3\pythonw.exe",
                Path.Combine(baseDir, @".venv\Scripts\python.exe"),
            };

            foreach (string p in candidates)
            {
                if (File.Exists(p))
                {
                    return p;
                }
            }
            return null;
        }

        private static void EnsureDesktopShortcut(string baseDir)
        {
            try
            {
                string desktopPath = Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory);
                string lnkPath = Path.Combine(desktopPath, "大模型量化交易.lnk");
                if (!File.Exists(lnkPath))
                {
                    CreateDesktopShortcut(baseDir);
                }
            }
            catch
            {
                // 静默忽略权限等异常
            }
        }

        public static void CreateDesktopShortcut(string baseDir)
        {
            try
            {
                string desktopPath = Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory);
                string lnkPath = Path.Combine(desktopPath, "大模型量化交易.lnk");
                string exePath = Process.GetCurrentProcess().MainModule.FileName;

                // 若 exePath 在开发中未确定，使用默认路径
                if (string.IsNullOrEmpty(exePath) || !File.Exists(exePath) || exePath.EndsWith(".vshost.exe", StringComparison.OrdinalIgnoreCase))
                {
                    exePath = Path.Combine(baseDir, "大模型量化交易.exe");
                }

                string icoPath = Path.Combine(baseDir, "app.ico");

                Type shellType = Type.GetTypeFromProgID("WScript.Shell");
                if (shellType != null)
                {
                    dynamic shell = Activator.CreateInstance(shellType);
                    dynamic shortcut = shell.CreateShortcut(lnkPath);
                    shortcut.TargetPath = exePath;
                    shortcut.WorkingDirectory = baseDir;
                    shortcut.Description = "大模型量化交易 · 模拟盘控制台";
                    if (File.Exists(icoPath))
                    {
                        shortcut.IconLocation = icoPath + ",0";
                    }
                    else
                    {
                        shortcut.IconLocation = exePath + ",0";
                    }
                    shortcut.Save();
                }
            }
            catch
            {
                // 忽略异常
            }
        }
    }
}
