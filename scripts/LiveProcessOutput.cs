using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.IO;
using System.Threading.Tasks;

// Pipe callbacks run without a PowerShell runspace. Only the UI timer touches controls.
public sealed class LabLiveProcessOutput
{
    private readonly ConcurrentQueue<string> chunks = new ConcurrentQueue<string>();
    private readonly Task readers;

    public LabLiveProcessOutput(StreamReader stdout, StreamReader stderr)
    {
        readers = Task.WhenAll(ReadPipe(stdout, false), ReadPipe(stderr, true));
    }

    private async Task ReadPipe(StreamReader reader, bool error)
    {
        char[] buffer = new char[4096];
        try
        {
            int count;
            while ((count = await reader.ReadAsync(buffer, 0, buffer.Length).ConfigureAwait(false)) > 0)
                chunks.Enqueue((error ? "[stderr] " : "") + new string(buffer, 0, count));
        }
        catch (Exception exception)
        {
            chunks.Enqueue("\n[출력 읽기 오류] " + exception.Message + "\n");
        }
    }

    public bool IsCompleted { get { return readers.IsCompleted; } }

    public string[] Drain()
    {
        var result = new List<string>();
        string chunk;
        while (chunks.TryDequeue(out chunk)) result.Add(chunk);
        return result.ToArray();
    }
}
