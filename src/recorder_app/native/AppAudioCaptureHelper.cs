using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;

[assembly: ComVisible(true)]

namespace LoopScribe.AppAudioCapture
{
    internal static class Program
    {
        private const int RequiredWindowsBuild = 20348;

        [STAThread]
        private static int Main(string[] args)
        {
            int coInit = NativeMethods.CoInitializeEx(IntPtr.Zero, NativeMethods.COINIT_APARTMENTTHREADED);
            try
            {
                if (args.Length == 0)
                {
                    WriteJsonError("missing_command");
                    return 2;
                }

                string command = args[0].ToLowerInvariant();
                if (command == "capability")
                {
                    return CapabilityCommand();
                }
                if (command == "smoke")
                {
                    return SmokeCommand();
                }
                if (command == "list")
                {
                    return ListCommand();
                }
                if (command == "capture")
                {
                    return CaptureCommand(args);
                }

                WriteJsonError("unknown_command");
                return 2;
            }
            catch (Exception ex)
            {
                WriteJsonError(ex.Message);
                return 1;
            }
            finally
            {
                if (coInit == 0 || coInit == 1)
                {
                    NativeMethods.CoUninitialize();
                }
            }
        }

        private static int CapabilityCommand()
        {
            string reason;
            VersionInfo version = NativeMethods.GetRealWindowsVersion();
            bool supported = IsSupported(version, out reason);
            Console.WriteLine("{\"ok\":true,\"supported\":" + Json.Bool(supported)
                + ",\"windows_build\":" + version.Build
                + ",\"reason\":\"" + Json.Escape(reason) + "\"}");
            return supported ? 0 : 3;
        }

        private static int SmokeCommand()
        {
            string reason;
            VersionInfo version = NativeMethods.GetRealWindowsVersion();
            if (!IsSupported(version, out reason))
            {
                Console.WriteLine("{\"ok\":false,\"supported\":false,\"reason\":\"" + Json.Escape(reason) + "\"}");
                return 3;
            }

            string tempPath = Path.Combine(Path.GetTempPath(), "loopscribe_app_audio_smoke_" + Guid.NewGuid().ToString("N") + ".wav");
            try
            {
                using (ProcessLoopbackCapture capture = new ProcessLoopbackCapture(
                    (uint)Process.GetCurrentProcess().Id,
                    tempPath,
                    48000))
                {
                    capture.Start();
                    Thread.Sleep(250);
                    capture.Stop();
                }
                Console.WriteLine("{\"ok\":true,\"supported\":true,\"reason\":\"process_loopback_smoke_ok\"}");
                return 0;
            }
            catch (Exception ex)
            {
                Console.WriteLine("{\"ok\":false,\"supported\":false,\"reason\":\"" + Json.Escape(ex.Message) + "\"}");
                return 4;
            }
            finally
            {
                try
                {
                    if (File.Exists(tempPath))
                    {
                        File.Delete(tempPath);
                    }
                }
                catch
                {
                }
            }
        }

        private static int ListCommand()
        {
            string reason;
            VersionInfo version = NativeMethods.GetRealWindowsVersion();
            if (!IsSupported(version, out reason))
            {
                Console.WriteLine("{\"ok\":false,\"sessions\":[],\"reason\":\"" + Json.Escape(reason) + "\"}");
                return 3;
            }

            List<AudioSessionInfo> sessions = AudioSessionEnumerator.ListAudioSessions();
            StringBuilder builder = new StringBuilder();
            builder.Append("{\"ok\":true,\"sessions\":[");
            for (int index = 0; index < sessions.Count; index++)
            {
                if (index > 0)
                {
                    builder.Append(',');
                }
                builder.Append(sessions[index].ToJson());
            }
            builder.Append("]}");
            Console.WriteLine(builder.ToString());
            return 0;
        }

        private static int CaptureCommand(string[] args)
        {
            uint pid = 0;
            string output = "";
            int sampleRate = 48000;
            for (int index = 1; index < args.Length; index++)
            {
                string item = args[index];
                if (item == "--pid" && index + 1 < args.Length)
                {
                    pid = UInt32.Parse(args[++index]);
                }
                else if (item == "--output" && index + 1 < args.Length)
                {
                    output = args[++index];
                }
                else if (item == "--sample-rate" && index + 1 < args.Length)
                {
                    sampleRate = Int32.Parse(args[++index]);
                }
            }

            if (pid == 0 || String.IsNullOrWhiteSpace(output))
            {
                Console.WriteLine("ERROR {\"ok\":false,\"error\":\"missing_pid_or_output\"}");
                return 2;
            }

            try
            {
                Process target = Process.GetProcessById((int)pid);
                using (ProcessLoopbackCapture capture = new ProcessLoopbackCapture(pid, output, sampleRate))
                {
                    capture.Start();
                    Console.WriteLine("READY");
                    Console.Out.Flush();

                    while (true)
                    {
                        if (Console.In.Peek() >= 0)
                        {
                            string line = Console.In.ReadLine();
                            if (String.Equals(line, "stop", StringComparison.OrdinalIgnoreCase))
                            {
                                break;
                            }
                        }
                        try
                        {
                            if (target.HasExited)
                            {
                                Console.Error.WriteLine("target_process_exited");
                                return 6;
                            }
                        }
                        catch
                        {
                            Console.Error.WriteLine("target_process_unavailable");
                            return 6;
                        }
                        Thread.Sleep(40);
                    }

                    capture.Stop();
                }

                Console.WriteLine("DONE");
                return 0;
            }
            catch (Exception ex)
            {
                Console.WriteLine("ERROR {\"ok\":false,\"error\":\"" + Json.Escape(ex.Message) + "\"}");
                return 5;
            }
        }

        private static bool IsSupported(VersionInfo version, out string reason)
        {
            if (Environment.OSVersion.Platform != PlatformID.Win32NT)
            {
                reason = "not_windows";
                return false;
            }
            if (version.Build < RequiredWindowsBuild)
            {
                reason = "windows_build_" + version.Build + "_below_" + RequiredWindowsBuild;
                return false;
            }
            IntPtr module = NativeMethods.LoadLibrary("Mmdevapi.dll");
            if (module == IntPtr.Zero)
            {
                reason = "mmdevapi_not_available";
                return false;
            }
            IntPtr proc = NativeMethods.GetProcAddress(module, "ActivateAudioInterfaceAsync");
            if (proc == IntPtr.Zero)
            {
                reason = "ActivateAudioInterfaceAsync_not_available";
                return false;
            }
            reason = "supported";
            return true;
        }

        private static void WriteJsonError(string message)
        {
            Console.WriteLine("{\"ok\":false,\"error\":\"" + Json.Escape(message) + "\"}");
        }
    }

    internal sealed class ProcessLoopbackCapture : IDisposable
    {
        private const string VirtualAudioDeviceProcessLoopback = "VAD\\Process_Loopback";
        private const int ActivationTypeProcessLoopback = 1;
        private const int ProcessLoopbackModeIncludeTargetProcessTree = 0;
        private const uint AudclntStreamflagsLoopback = 0x00020000;
        private const uint AudclntStreamflagsEventCallback = 0x00040000;
        private const uint AudclntStreamflagsAutoconvertPcm = 0x80000000;
        private const uint AudclntStreamflagsSrcDefaultQuality = 0x08000000;
        private const int AudclntSharemodeShared = 0;
        private const int AudclntBufferflagsSilent = 0x2;
        private static readonly Guid IAudioClientGuid = new Guid("1CB9AD4C-DBFA-4c32-B178-C2F568A703B2");
        private static readonly Guid IAudioCaptureClientGuid = new Guid("C8ADBD64-E71E-48a0-A4DE-185C395CD317");

        private readonly uint targetProcessId;
        private readonly string outputPath;
        private readonly int requestedSampleRate;
        private AudioClientNative audioClient;
        private AudioCaptureClientNative captureClient;
        private IntPtr captureEvent = IntPtr.Zero;
        private Thread captureThread;
        private volatile bool stopRequested;
        private Exception captureError;
        private WaveFileWriter writer;
        private WaveFormatEx format;

        public ProcessLoopbackCapture(uint targetProcessId, string outputPath, int requestedSampleRate)
        {
            this.targetProcessId = targetProcessId;
            this.outputPath = outputPath;
            this.requestedSampleRate = requestedSampleRate > 0 ? requestedSampleRate : 48000;
        }

        public void Start()
        {
            Directory.CreateDirectory(Path.GetDirectoryName(Path.GetFullPath(outputPath)));
            audioClient = ActivateAudioClient();
            format = InitializeAudioClient(audioClient, requestedSampleRate);
            IntPtr serviceObject;
            Guid captureClientGuid = IAudioCaptureClientGuid;
            int hr = audioClient.GetService(ref captureClientGuid, out serviceObject);
            HResult.Check(hr, "IAudioClient.GetService");
            captureClient = new AudioCaptureClientNative(serviceObject);
            writer = new WaveFileWriter(outputPath, format);

            hr = audioClient.Start();
            HResult.Check(hr, "IAudioClient.Start");

            stopRequested = false;
            captureThread = new Thread(CaptureLoop);
            captureThread.IsBackground = true;
            captureThread.Name = "LoopScribeAppAudioCapture";
            captureThread.Start();
        }

        public void Stop()
        {
            stopRequested = true;
            if (captureEvent != IntPtr.Zero)
            {
                NativeMethods.SetEvent(captureEvent);
            }
            if (captureThread != null)
            {
                captureThread.Join(5000);
            }
            if (captureError != null)
            {
                throw new InvalidOperationException("capture_loop_failed: " + captureError.Message, captureError);
            }
            if (audioClient != null)
            {
                audioClient.Stop();
            }
            if (writer != null)
            {
                writer.Dispose();
                writer = null;
            }
        }

        private AudioClientNative ActivateAudioClient()
        {
            using (ActivationParamsMemory activationParams = new ActivationParamsMemory(targetProcessId))
            {
                ActivationHandler handler = new ActivationHandler();
                IActivateAudioInterfaceAsyncOperation operation;
                Guid audioClientGuid = IAudioClientGuid;
                int hr = NativeMethods.ActivateAudioInterfaceAsync(
                    VirtualAudioDeviceProcessLoopback,
                    ref audioClientGuid,
                    activationParams.PropVariantPtr,
                    handler,
                    out operation);
                HResult.Check(hr, "ActivateAudioInterfaceAsync");
                if (!handler.Wait(5000))
                {
                    throw new TimeoutException("Process loopback activation timed out.");
                }
                HResult.Check(handler.ActivateResult, "IActivateAudioInterfaceAsyncOperation.GetActivateResult");
                return new AudioClientNative(handler.AudioClientPtr);
            }
        }

        private WaveFormatEx InitializeAudioClient(AudioClientNative client, int desiredSampleRate)
        {
            int[] sampleRates = desiredSampleRate == 44100
                ? new int[] { 44100, 48000 }
                : new int[] { desiredSampleRate, 44100 };
            int lastHr = 0;
            for (int index = 0; index < sampleRates.Length; index++)
            {
                WaveFormatEx candidate = WaveFormatEx.Pcm(sampleRates[index], 2, 16);
                IntPtr formatPtr = Marshal.AllocHGlobal(Marshal.SizeOf(typeof(WaveFormatEx)));
                try
                {
                    Marshal.StructureToPtr(candidate, formatPtr, false);
                    captureEvent = NativeMethods.CreateEvent(IntPtr.Zero, false, false, null);
                    if (captureEvent == IntPtr.Zero)
                    {
                        throw new InvalidOperationException("CreateEvent failed.");
                    }
                    Guid sessionGuid = Guid.Empty;
                    uint flags = AudclntStreamflagsLoopback
                        | AudclntStreamflagsEventCallback
                        | AudclntStreamflagsAutoconvertPcm
                        | AudclntStreamflagsSrcDefaultQuality;
                    int hr = client.Initialize(
                        AudclntSharemodeShared,
                        flags,
                        10000000,
                        0,
                        formatPtr,
                        ref sessionGuid);
                    if (hr == 0)
                    {
                        hr = client.SetEventHandle(captureEvent);
                        HResult.Check(hr, "IAudioClient.SetEventHandle");
                        return candidate;
                    }
                    lastHr = hr;
                    NativeMethods.CloseHandle(captureEvent);
                    captureEvent = IntPtr.Zero;
                }
                finally
                {
                    Marshal.FreeHGlobal(formatPtr);
                }
            }
            HResult.Check(lastHr, "IAudioClient.Initialize");
            throw new InvalidOperationException("No supported capture format found.");
        }

        private void CaptureLoop()
        {
            byte[] silence = new byte[format.BlockAlign * 4096];
            try
            {
                while (!stopRequested)
                {
                    NativeMethods.WaitForSingleObject(captureEvent, 250);
                    uint nextPacketSize;
                    int hr = captureClient.GetNextPacketSize(out nextPacketSize);
                    HResult.Check(hr, "IAudioCaptureClient.GetNextPacketSize");

                    while (nextPacketSize > 0)
                    {
                        IntPtr data;
                        uint framesAvailable;
                        int flags;
                        ulong devicePosition;
                        ulong qpcPosition;
                        hr = captureClient.GetBuffer(
                            out data,
                            out framesAvailable,
                            out flags,
                            out devicePosition,
                            out qpcPosition);
                        HResult.Check(hr, "IAudioCaptureClient.GetBuffer");

                        int byteCount = checked((int)(framesAvailable * (uint)format.BlockAlign));
                        if ((flags & AudclntBufferflagsSilent) != 0 || data == IntPtr.Zero)
                        {
                            WriteSilence(byteCount, silence);
                        }
                        else
                        {
                            byte[] buffer = new byte[byteCount];
                            Marshal.Copy(data, buffer, 0, byteCount);
                            writer.Write(buffer, 0, byteCount);
                        }

                        hr = captureClient.ReleaseBuffer(framesAvailable);
                        HResult.Check(hr, "IAudioCaptureClient.ReleaseBuffer");
                        hr = captureClient.GetNextPacketSize(out nextPacketSize);
                        HResult.Check(hr, "IAudioCaptureClient.GetNextPacketSize");
                    }
                }
            }
            catch (Exception ex)
            {
                captureError = ex;
            }
        }

        private void WriteSilence(int byteCount, byte[] silence)
        {
            int remaining = byteCount;
            while (remaining > 0)
            {
                int chunk = Math.Min(remaining, silence.Length);
                writer.Write(silence, 0, chunk);
                remaining -= chunk;
            }
        }

        public void Dispose()
        {
            try
            {
                if (writer != null)
                {
                    writer.Dispose();
                    writer = null;
                }
                if (captureClient != null)
                {
                    captureClient.Dispose();
                    captureClient = null;
                }
                if (audioClient != null)
                {
                    audioClient.Dispose();
                    audioClient = null;
                }
            }
            finally
            {
                if (captureEvent != IntPtr.Zero)
                {
                    NativeMethods.CloseHandle(captureEvent);
                    captureEvent = IntPtr.Zero;
                }
            }
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct AudioClientProcessLoopbackParams
        {
            public uint TargetProcessId;
            public int ProcessLoopbackMode;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct AudioClientActivationParams
        {
            public int ActivationType;
            public AudioClientProcessLoopbackParams ProcessLoopbackParams;
        }

        private sealed class ActivationParamsMemory : IDisposable
        {
            public readonly IntPtr PropVariantPtr;
            private readonly IntPtr activationParamsPtr;

            public ActivationParamsMemory(uint targetProcessId)
            {
                AudioClientActivationParams activationParams = new AudioClientActivationParams();
                activationParams.ActivationType = ActivationTypeProcessLoopback;
                activationParams.ProcessLoopbackParams.TargetProcessId = targetProcessId;
                activationParams.ProcessLoopbackParams.ProcessLoopbackMode = ProcessLoopbackModeIncludeTargetProcessTree;

                activationParamsPtr = Marshal.AllocHGlobal(Marshal.SizeOf(typeof(AudioClientActivationParams)));
                Marshal.StructureToPtr(activationParams, activationParamsPtr, false);

                PropVariant propVariant = new PropVariant();
                propVariant.vt = NativeMethods.VT_BLOB;
                propVariant.blob.cbSize = Marshal.SizeOf(typeof(AudioClientActivationParams));
                propVariant.blob.pBlobData = activationParamsPtr;
                PropVariantPtr = Marshal.AllocHGlobal(Marshal.SizeOf(typeof(PropVariant)));
                Marshal.StructureToPtr(propVariant, PropVariantPtr, false);
            }

            public void Dispose()
            {
                Marshal.FreeHGlobal(PropVariantPtr);
                Marshal.FreeHGlobal(activationParamsPtr);
            }
        }
    }

    internal static class AudioSessionEnumerator
    {
        private const uint DeviceStateActive = 0x00000001;
        private const int DataFlowRender = 0;
        private const uint ClsctxAll = 23;
        private static readonly Guid AudioSessionManager2Guid = new Guid("77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F");

        public static List<AudioSessionInfo> ListAudioSessions()
        {
            Dictionary<uint, AudioSessionInfo> byPid = new Dictionary<uint, AudioSessionInfo>();
            IMMDeviceEnumerator deviceEnumerator = (IMMDeviceEnumerator)(new MMDeviceEnumeratorComObject());
            IMMDeviceCollection devices;
            int hr = deviceEnumerator.EnumAudioEndpoints(DataFlowRender, DeviceStateActive, out devices);
            HResult.Check(hr, "IMMDeviceEnumerator.EnumAudioEndpoints");
            uint deviceCount;
            HResult.Check(devices.GetCount(out deviceCount), "IMMDeviceCollection.GetCount");

            for (uint deviceIndex = 0; deviceIndex < deviceCount; deviceIndex++)
            {
                IMMDevice device;
                if (devices.Item(deviceIndex, out device) != 0 || device == null)
                {
                    continue;
                }
                object managerObject;
                Guid managerGuid = AudioSessionManager2Guid;
                if (device.Activate(ref managerGuid, ClsctxAll, IntPtr.Zero, out managerObject) != 0)
                {
                    continue;
                }
                IAudioSessionManager2 manager = managerObject as IAudioSessionManager2;
                if (manager == null)
                {
                    continue;
                }
                IAudioSessionEnumerator sessionEnumerator;
                if (manager.GetSessionEnumerator(out sessionEnumerator) != 0 || sessionEnumerator == null)
                {
                    continue;
                }
                int sessionCount;
                if (sessionEnumerator.GetCount(out sessionCount) != 0)
                {
                    continue;
                }
                for (int sessionIndex = 0; sessionIndex < sessionCount; sessionIndex++)
                {
                    IAudioSessionControl2 session;
                    if (sessionEnumerator.GetSession(sessionIndex, out session) != 0 || session == null)
                    {
                        continue;
                    }
                    uint pid;
                    if (session.GetProcessId(out pid) != 0 || pid == 0)
                    {
                        continue;
                    }
                    int state;
                    if (session.GetState(out state) != 0 || state == 2)
                    {
                        continue;
                    }
                    string displayName = "";
                    session.GetDisplayName(out displayName);

                    AudioSessionInfo info = BuildInfo(pid, displayName, state);
                    AudioSessionInfo existing;
                    if (byPid.TryGetValue(pid, out existing))
                    {
                        existing.Merge(info);
                    }
                    else
                    {
                        byPid[pid] = info;
                    }
                }
            }

            List<AudioSessionInfo> result = new List<AudioSessionInfo>(byPid.Values);
            result.Sort(delegate(AudioSessionInfo left, AudioSessionInfo right)
            {
                int activeCompare = right.IsActive.CompareTo(left.IsActive);
                if (activeCompare != 0)
                {
                    return activeCompare;
                }
                return StringComparer.CurrentCultureIgnoreCase.Compare(left.DisplayName, right.DisplayName);
            });
            return result;
        }

        private static AudioSessionInfo BuildInfo(uint pid, string displayName, int state)
        {
            string processName = "PID " + pid;
            string path = "";
            try
            {
                Process process = Process.GetProcessById((int)pid);
                processName = process.ProcessName;
                try
                {
                    path = process.MainModule.FileName;
                }
                catch
                {
                    path = "";
                }
            }
            catch
            {
            }
            if (String.IsNullOrWhiteSpace(displayName))
            {
                displayName = processName;
            }
            return new AudioSessionInfo(pid, processName, displayName, path, state);
        }
    }

    internal sealed class AudioSessionInfo
    {
        public readonly uint Pid;
        public readonly string ProcessName;
        public string DisplayName;
        public readonly string ExecutablePath;
        public int State;

        public AudioSessionInfo(uint pid, string processName, string displayName, string executablePath, int state)
        {
            Pid = pid;
            ProcessName = processName;
            DisplayName = displayName;
            ExecutablePath = executablePath;
            State = state;
        }

        public bool IsActive
        {
            get { return State == 1; }
        }

        public void Merge(AudioSessionInfo other)
        {
            if (other.State == 1)
            {
                State = 1;
            }
            if (String.IsNullOrWhiteSpace(DisplayName) || DisplayName.StartsWith("PID ", StringComparison.Ordinal))
            {
                DisplayName = other.DisplayName;
            }
        }

        public string ToJson()
        {
            string stateText = State == 1 ? "Active" : "Inactive";
            string id = Pid + "|" + ExecutablePath;
            return "{\"id\":\"" + Json.Escape(id)
                + "\",\"pid\":" + Pid
                + ",\"process_name\":\"" + Json.Escape(ProcessName)
                + "\",\"display_name\":\"" + Json.Escape(DisplayName)
                + "\",\"executable_path\":\"" + Json.Escape(ExecutablePath)
                + "\",\"state\":\"" + Json.Escape(stateText) + "\"}";
        }
    }

    internal sealed class WaveFileWriter : IDisposable
    {
        private readonly FileStream stream;
        private readonly BinaryWriter writer;
        private readonly WaveFormatEx format;
        private long dataBytes;

        public WaveFileWriter(string path, WaveFormatEx format)
        {
            this.format = format;
            stream = new FileStream(path, FileMode.Create, FileAccess.Write, FileShare.Read);
            writer = new BinaryWriter(stream);
            WriteHeader(0);
        }

        public void Write(byte[] buffer, int offset, int count)
        {
            writer.Write(buffer, offset, count);
            dataBytes += count;
        }

        public void Dispose()
        {
            if (writer == null)
            {
                return;
            }
            writer.Flush();
            stream.Seek(0, SeekOrigin.Begin);
            WriteHeader(dataBytes);
            writer.Dispose();
            stream.Dispose();
        }

        private void WriteHeader(long dataLength)
        {
            writer.Write(Encoding.ASCII.GetBytes("RIFF"));
            writer.Write((uint)(36 + dataLength));
            writer.Write(Encoding.ASCII.GetBytes("WAVE"));
            writer.Write(Encoding.ASCII.GetBytes("fmt "));
            writer.Write((uint)16);
            writer.Write((ushort)format.FormatTag);
            writer.Write((ushort)format.Channels);
            writer.Write((uint)format.SamplesPerSec);
            writer.Write((uint)format.AvgBytesPerSec);
            writer.Write((ushort)format.BlockAlign);
            writer.Write((ushort)format.BitsPerSample);
            writer.Write(Encoding.ASCII.GetBytes("data"));
            writer.Write((uint)dataLength);
        }
    }

    [StructLayout(LayoutKind.Sequential)]
    internal struct WaveFormatEx
    {
        public ushort FormatTag;
        public ushort Channels;
        public uint SamplesPerSec;
        public uint AvgBytesPerSec;
        public ushort BlockAlign;
        public ushort BitsPerSample;
        public ushort Size;

        public static WaveFormatEx Pcm(int sampleRate, ushort channels, ushort bitsPerSample)
        {
            WaveFormatEx format = new WaveFormatEx();
            format.FormatTag = 1;
            format.Channels = channels;
            format.SamplesPerSec = (uint)sampleRate;
            format.BitsPerSample = bitsPerSample;
            format.BlockAlign = (ushort)(channels * bitsPerSample / 8);
            format.AvgBytesPerSec = format.SamplesPerSec * format.BlockAlign;
            format.Size = 0;
            return format;
        }
    }

    [StructLayout(LayoutKind.Sequential)]
    internal struct Blob
    {
        public int cbSize;
        public IntPtr pBlobData;
    }

    [StructLayout(LayoutKind.Explicit)]
    internal struct PropVariant
    {
        [FieldOffset(0)]
        public ushort vt;
        [FieldOffset(8)]
        public Blob blob;
    }

    internal sealed class AudioClientNative : IDisposable
    {
        private IntPtr pointer;

        public AudioClientNative(IntPtr pointer)
        {
            if (pointer == IntPtr.Zero)
            {
                throw new ArgumentNullException("pointer");
            }
            this.pointer = pointer;
        }

        public int Initialize(int shareMode, uint streamFlags, long bufferDuration, long periodicity, IntPtr format, ref Guid audioSessionGuid)
        {
            return ComDelegates.Get<AudioClientInitializeDelegate>(pointer, 3)(
                pointer,
                shareMode,
                streamFlags,
                bufferDuration,
                periodicity,
                format,
                ref audioSessionGuid);
        }

        public int Start()
        {
            return ComDelegates.Get<AudioClientNoArgsDelegate>(pointer, 10)(pointer);
        }

        public int Stop()
        {
            return ComDelegates.Get<AudioClientNoArgsDelegate>(pointer, 11)(pointer);
        }

        public int SetEventHandle(IntPtr eventHandle)
        {
            return ComDelegates.Get<AudioClientSetEventHandleDelegate>(pointer, 13)(pointer, eventHandle);
        }

        public int GetService(ref Guid interfaceId, out IntPtr service)
        {
            return ComDelegates.Get<AudioClientGetServiceDelegate>(pointer, 14)(pointer, ref interfaceId, out service);
        }

        public void Dispose()
        {
            if (pointer != IntPtr.Zero)
            {
                ComDelegates.Release(pointer);
                pointer = IntPtr.Zero;
            }
        }
    }

    internal sealed class AudioCaptureClientNative : IDisposable
    {
        private IntPtr pointer;

        public AudioCaptureClientNative(IntPtr pointer)
        {
            if (pointer == IntPtr.Zero)
            {
                throw new ArgumentNullException("pointer");
            }
            this.pointer = pointer;
        }

        public int GetBuffer(out IntPtr data, out uint framesAvailable, out int flags, out ulong devicePosition, out ulong qpcPosition)
        {
            return ComDelegates.Get<AudioCaptureClientGetBufferDelegate>(pointer, 3)(
                pointer,
                out data,
                out framesAvailable,
                out flags,
                out devicePosition,
                out qpcPosition);
        }

        public int ReleaseBuffer(uint framesRead)
        {
            return ComDelegates.Get<AudioCaptureClientReleaseBufferDelegate>(pointer, 4)(pointer, framesRead);
        }

        public int GetNextPacketSize(out uint nextPacketSize)
        {
            return ComDelegates.Get<AudioCaptureClientGetNextPacketSizeDelegate>(pointer, 5)(pointer, out nextPacketSize);
        }

        public void Dispose()
        {
            if (pointer != IntPtr.Zero)
            {
                ComDelegates.Release(pointer);
                pointer = IntPtr.Zero;
            }
        }
    }

    internal static class ComDelegates
    {
        public static T Get<T>(IntPtr comPointer, int slot) where T : class
        {
            IntPtr vtable = Marshal.ReadIntPtr(comPointer);
            IntPtr function = Marshal.ReadIntPtr(vtable, slot * IntPtr.Size);
            return Marshal.GetDelegateForFunctionPointer(function, typeof(T)) as T;
        }

        public static void Release(IntPtr comPointer)
        {
            ReleaseDelegate release = Get<ReleaseDelegate>(comPointer, 2);
            release(comPointer);
        }
    }

    [UnmanagedFunctionPointer(CallingConvention.StdCall)]
    internal delegate uint ReleaseDelegate(IntPtr thisPtr);

    [UnmanagedFunctionPointer(CallingConvention.StdCall)]
    internal delegate int AudioClientInitializeDelegate(
        IntPtr thisPtr,
        int shareMode,
        uint streamFlags,
        long bufferDuration,
        long periodicity,
        IntPtr format,
        ref Guid audioSessionGuid);

    [UnmanagedFunctionPointer(CallingConvention.StdCall)]
    internal delegate int AudioClientNoArgsDelegate(IntPtr thisPtr);

    [UnmanagedFunctionPointer(CallingConvention.StdCall)]
    internal delegate int AudioClientSetEventHandleDelegate(IntPtr thisPtr, IntPtr eventHandle);

    [UnmanagedFunctionPointer(CallingConvention.StdCall)]
    internal delegate int AudioClientGetServiceDelegate(IntPtr thisPtr, ref Guid interfaceId, out IntPtr service);

    [UnmanagedFunctionPointer(CallingConvention.StdCall)]
    internal delegate int AudioCaptureClientGetBufferDelegate(
        IntPtr thisPtr,
        out IntPtr data,
        out uint framesAvailable,
        out int flags,
        out ulong devicePosition,
        out ulong qpcPosition);

    [UnmanagedFunctionPointer(CallingConvention.StdCall)]
    internal delegate int AudioCaptureClientReleaseBufferDelegate(IntPtr thisPtr, uint framesRead);

    [UnmanagedFunctionPointer(CallingConvention.StdCall)]
    internal delegate int AudioCaptureClientGetNextPacketSizeDelegate(IntPtr thisPtr, out uint nextPacketSize);

    [ComVisible(true)]
    public sealed class ActivationHandler : IActivateAudioInterfaceCompletionHandler, IAgileObject
    {
        private readonly ManualResetEventSlim completed = new ManualResetEventSlim(false);
        internal int ActivateResult;
        internal IntPtr AudioClientPtr;

        public int ActivateCompleted(IActivateAudioInterfaceAsyncOperation operation)
        {
            try
            {
                IntPtr activatedInterface;
                int getResultHr = operation.GetActivateResult(out ActivateResult, out activatedInterface);
                if (getResultHr < 0)
                {
                    ActivateResult = getResultHr;
                    return 0;
                }
                if (ActivateResult == 0)
                {
                    AudioClientPtr = activatedInterface;
                }
            }
            catch (Exception ex)
            {
                ActivateResult = Marshal.GetHRForException(ex);
            }
            finally
            {
                completed.Set();
            }
            return 0;
        }

        public bool Wait(int milliseconds)
        {
            return completed.Wait(milliseconds);
        }
    }

    [ComVisible(true)]
    [Guid("94EA2B94-E9CC-49E0-C0FF-EE64CA8F5B90")]
    [InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    public interface IAgileObject
    {
    }

    internal static class HResult
    {
        public static void Check(int hr, string operation)
        {
            if (hr < 0)
            {
                throw new COMException(operation + " failed with HRESULT 0x" + hr.ToString("X8"), hr);
            }
        }
    }

    internal struct VersionInfo
    {
        public int Major;
        public int Minor;
        public int Build;
    }

    internal static class NativeMethods
    {
        public const ushort VT_BLOB = 65;
        public const uint COINIT_MULTITHREADED = 0;
        public const uint COINIT_APARTMENTTHREADED = 2;
        public const int WAIT_OBJECT_0 = 0;

        [DllImport("ole32.dll")]
        public static extern int CoInitializeEx(IntPtr pvReserved, uint dwCoInit);

        [DllImport("ole32.dll")]
        public static extern void CoUninitialize();

        [DllImport("Mmdevapi.dll", ExactSpelling = true, PreserveSig = true)]
        public static extern int ActivateAudioInterfaceAsync(
            [MarshalAs(UnmanagedType.LPWStr)] string deviceInterfacePath,
            [In] ref Guid riid,
            IntPtr activationParams,
            IActivateAudioInterfaceCompletionHandler completionHandler,
            out IActivateAudioInterfaceAsyncOperation activationOperation);

        [DllImport("kernel32.dll", SetLastError = true)]
        public static extern IntPtr CreateEvent(IntPtr lpEventAttributes, bool bManualReset, bool bInitialState, string lpName);

        [DllImport("kernel32.dll", SetLastError = true)]
        public static extern bool SetEvent(IntPtr hEvent);

        [DllImport("kernel32.dll", SetLastError = true)]
        public static extern bool CloseHandle(IntPtr hObject);

        [DllImport("kernel32.dll", SetLastError = true)]
        public static extern uint WaitForSingleObject(IntPtr hHandle, uint dwMilliseconds);

        [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Ansi)]
        public static extern IntPtr LoadLibrary(string lpFileName);

        [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Ansi)]
        public static extern IntPtr GetProcAddress(IntPtr hModule, string procName);

        [DllImport("ntdll.dll", SetLastError = true)]
        private static extern int RtlGetVersion(ref RTL_OSVERSIONINFOEX versionInfo);

        public static VersionInfo GetRealWindowsVersion()
        {
            RTL_OSVERSIONINFOEX info = new RTL_OSVERSIONINFOEX();
            info.dwOSVersionInfoSize = Marshal.SizeOf(typeof(RTL_OSVERSIONINFOEX));
            if (RtlGetVersion(ref info) != 0)
            {
                Version fallback = Environment.OSVersion.Version;
                return new VersionInfo { Major = fallback.Major, Minor = fallback.Minor, Build = fallback.Build };
            }
            return new VersionInfo { Major = (int)info.dwMajorVersion, Minor = (int)info.dwMinorVersion, Build = (int)info.dwBuildNumber };
        }

        [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
        private struct RTL_OSVERSIONINFOEX
        {
            public int dwOSVersionInfoSize;
            public uint dwMajorVersion;
            public uint dwMinorVersion;
            public uint dwBuildNumber;
            public uint dwPlatformId;
            [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 128)]
            public string szCSDVersion;
        }
    }

    [ComImport]
    [Guid("41D949AB-9862-444A-80F6-C261334DA5EB")]
    [InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    public interface IActivateAudioInterfaceCompletionHandler
    {
        [PreserveSig]
        int ActivateCompleted(IActivateAudioInterfaceAsyncOperation activateOperation);
    }

    [ComImport]
    [Guid("72A22D78-CDE4-431D-B8CC-843A71199B6D")]
    [InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    public interface IActivateAudioInterfaceAsyncOperation
    {
        [PreserveSig]
        int GetActivateResult(out int activateResult, out IntPtr activatedInterface);
    }

    [ComImport]
    [Guid("1CB9AD4C-DBFA-4c32-B178-C2F568A703B2")]
    [InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    public interface IAudioClient
    {
        [PreserveSig]
        int Initialize(int shareMode, uint streamFlags, long bufferDuration, long periodicity, IntPtr format, ref Guid audioSessionGuid);
        [PreserveSig]
        int GetBufferSize(out uint bufferSize);
        [PreserveSig]
        int GetStreamLatency(out long latency);
        [PreserveSig]
        int GetCurrentPadding(out uint currentPadding);
        [PreserveSig]
        int IsFormatSupported(int shareMode, IntPtr format, out IntPtr closestMatch);
        [PreserveSig]
        int GetMixFormat(out IntPtr deviceFormat);
        [PreserveSig]
        int GetDevicePeriod(out long defaultDevicePeriod, out long minimumDevicePeriod);
        [PreserveSig]
        int Start();
        [PreserveSig]
        int Stop();
        [PreserveSig]
        int Reset();
        [PreserveSig]
        int SetEventHandle(IntPtr eventHandle);
        [PreserveSig]
        int GetService(ref Guid interfaceId, [MarshalAs(UnmanagedType.IUnknown)] out object service);
    }

    [ComImport]
    [Guid("C8ADBD64-E71E-48a0-A4DE-185C395CD317")]
    [InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    internal interface IAudioCaptureClient
    {
        [PreserveSig]
        int GetBuffer(out IntPtr data, out uint framesAvailable, out int flags, out ulong devicePosition, out ulong qpcPosition);
        [PreserveSig]
        int ReleaseBuffer(uint framesRead);
        [PreserveSig]
        int GetNextPacketSize(out uint nextPacketSize);
    }

    [ComImport]
    [Guid("BCDE0395-E52F-467C-8E3D-C4579291692E")]
    internal class MMDeviceEnumeratorComObject
    {
    }

    [ComImport]
    [Guid("A95664D2-9614-4F35-A746-DE8DB63617E6")]
    [InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    internal interface IMMDeviceEnumerator
    {
        [PreserveSig]
        int EnumAudioEndpoints(int dataFlow, uint stateMask, [MarshalAs(UnmanagedType.Interface)] out IMMDeviceCollection devices);
        [PreserveSig]
        int GetDefaultAudioEndpoint(int dataFlow, int role, [MarshalAs(UnmanagedType.Interface)] out IMMDevice endpoint);
        [PreserveSig]
        int GetDevice([MarshalAs(UnmanagedType.LPWStr)] string id, [MarshalAs(UnmanagedType.Interface)] out IMMDevice device);
        [PreserveSig]
        int RegisterEndpointNotificationCallback(IntPtr client);
        [PreserveSig]
        int UnregisterEndpointNotificationCallback(IntPtr client);
    }

    [ComImport]
    [Guid("0BD7A1BE-7A1A-44DB-8397-CC5392387B5E")]
    [InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    internal interface IMMDeviceCollection
    {
        [PreserveSig]
        int GetCount(out uint count);
        [PreserveSig]
        int Item(uint index, [MarshalAs(UnmanagedType.Interface)] out IMMDevice device);
    }

    [ComImport]
    [Guid("D666063F-1587-4E43-81F1-B948E807363F")]
    [InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    internal interface IMMDevice
    {
        [PreserveSig]
        int Activate(ref Guid interfaceId, uint clsCtx, IntPtr activationParams, [MarshalAs(UnmanagedType.IUnknown)] out object interfaceObject);
        [PreserveSig]
        int OpenPropertyStore(int access, out object properties);
        [PreserveSig]
        int GetId([MarshalAs(UnmanagedType.LPWStr)] out string id);
        [PreserveSig]
        int GetState(out uint state);
    }

    [ComImport]
    [Guid("77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F")]
    [InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    internal interface IAudioSessionManager2
    {
        [PreserveSig]
        int GetAudioSessionControl(IntPtr audioSessionGuid, uint streamFlags, out object sessionControl);
        [PreserveSig]
        int GetSimpleAudioVolume(IntPtr audioSessionGuid, uint streamFlags, out object audioVolume);
        [PreserveSig]
        int GetSessionEnumerator([MarshalAs(UnmanagedType.Interface)] out IAudioSessionEnumerator sessionEnumerator);
        [PreserveSig]
        int RegisterSessionNotification(IntPtr sessionNotification);
        [PreserveSig]
        int UnregisterSessionNotification(IntPtr sessionNotification);
        [PreserveSig]
        int RegisterDuckNotification([MarshalAs(UnmanagedType.LPWStr)] string sessionId, IntPtr duckNotification);
        [PreserveSig]
        int UnregisterDuckNotification(IntPtr duckNotification);
    }

    [ComImport]
    [Guid("E2F5BB11-0570-40CA-ACDD-3AA01277DEE8")]
    [InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    internal interface IAudioSessionEnumerator
    {
        [PreserveSig]
        int GetCount(out int sessionCount);
        [PreserveSig]
        int GetSession(int sessionIndex, [MarshalAs(UnmanagedType.Interface)] out IAudioSessionControl2 session);
    }

    [ComImport]
    [Guid("BFB7FF88-7239-4FC9-8FA2-07C950BE9C6D")]
    [InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    internal interface IAudioSessionControl2
    {
        [PreserveSig]
        int GetState(out int state);
        [PreserveSig]
        int GetDisplayName([MarshalAs(UnmanagedType.LPWStr)] out string displayName);
        [PreserveSig]
        int SetDisplayName([MarshalAs(UnmanagedType.LPWStr)] string displayName, ref Guid eventContext);
        [PreserveSig]
        int GetIconPath([MarshalAs(UnmanagedType.LPWStr)] out string iconPath);
        [PreserveSig]
        int SetIconPath([MarshalAs(UnmanagedType.LPWStr)] string iconPath, ref Guid eventContext);
        [PreserveSig]
        int GetGroupingParam(out Guid groupingId);
        [PreserveSig]
        int SetGroupingParam(ref Guid groupingId, ref Guid eventContext);
        [PreserveSig]
        int RegisterAudioSessionNotification(IntPtr notification);
        [PreserveSig]
        int UnregisterAudioSessionNotification(IntPtr notification);
        [PreserveSig]
        int GetSessionIdentifier([MarshalAs(UnmanagedType.LPWStr)] out string sessionId);
        [PreserveSig]
        int GetSessionInstanceIdentifier([MarshalAs(UnmanagedType.LPWStr)] out string sessionInstanceId);
        [PreserveSig]
        int GetProcessId(out uint processId);
        [PreserveSig]
        int IsSystemSoundsSession();
        [PreserveSig]
        int SetDuckingPreference(bool optOut);
    }

    internal static class Json
    {
        public static string Bool(bool value)
        {
            return value ? "true" : "false";
        }

        public static string Escape(string value)
        {
            if (value == null)
            {
                return "";
            }
            StringBuilder builder = new StringBuilder(value.Length + 8);
            for (int index = 0; index < value.Length; index++)
            {
                char c = value[index];
                switch (c)
                {
                    case '\\':
                        builder.Append("\\\\");
                        break;
                    case '"':
                        builder.Append("\\\"");
                        break;
                    case '\r':
                        builder.Append("\\r");
                        break;
                    case '\n':
                        builder.Append("\\n");
                        break;
                    case '\t':
                        builder.Append("\\t");
                        break;
                    default:
                        if (c < 32)
                        {
                            builder.Append("\\u");
                            builder.Append(((int)c).ToString("x4"));
                        }
                        else
                        {
                            builder.Append(c);
                        }
                        break;
                }
            }
            return builder.ToString();
        }
    }
}
