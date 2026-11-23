using System.Text.Json;
using MegaCrit.Sts2.Core.Saves;
using MegaCrit.Sts2.Core.Saves.Managers;

namespace ShoujoKageki.Core.Runtime.Modules.Shine.Saves;

/// <summary>Keeps the serialized checkpoint's snapshots with the native atomic run-file write.</summary>
internal sealed class CoreRunSaveWriteScope : IDisposable
{
    private static readonly AsyncLocal<CoreRunSaveWriteScope?> Current = new();
    private readonly CoreRunSaveWriteScope? _parent;
    private readonly string _fileName;
    private readonly Dictionary<ulong, string> _snapshots = [];

    internal CoreRunSaveWriteScope(SerializableRun run, bool multiplayer)
    {
        _parent = Current.Value;
        _fileName = multiplayer ? RunSaveManager.multiplayerRunSaveFileName : RunSaveManager.runSaveFileName;
        foreach (var player in run.Players)
        {
            if (CorePlayerSave.GetSerializedSnapshot(player) is { } snapshot)
                _snapshots.Add(player.NetId, snapshot);
        }
        Current.Value = this;
    }

    internal static byte[] IncludeSnapshots(string path, byte[] bytes)
    {
        if (Current.Value is not { } scope || scope._snapshots.Count == 0 || Path.GetFileName(path) != scope._fileName)
            return bytes;

        using var doc = JsonDocument.Parse(bytes);
        var root = doc.RootElement;
        if (!root.TryGetProperty("players", out var players) || players.ValueKind != JsonValueKind.Array
            || !players.EnumerateArray().Any(scope.NeedsSnapshot))
            return bytes;

        using var stream = new MemoryStream(bytes.Length);
        using var writer = new Utf8JsonWriter(stream, new JsonWriterOptions { Indented = true });
        writer.WriteStartObject();
        foreach (var property in root.EnumerateObject())
        {
            if (!property.NameEquals("players"))
            {
                property.WriteTo(writer);
                continue;
            }

            writer.WriteStartArray("players");
            foreach (var player in players.EnumerateArray())
                scope.WritePlayer(writer, player);
            writer.WriteEndArray();
        }
        writer.WriteEndObject();
        writer.Flush();
        return stream.ToArray();
    }

    private bool NeedsSnapshot(JsonElement player)
        => player.TryGetProperty("net_id", out var id) && id.TryGetUInt64(out var netId) && _snapshots.ContainsKey(netId)
           && (!player.TryGetProperty(CorePlayerSave.SaveDictionary, out var saves) || saves.ValueKind == JsonValueKind.Null
               || (saves.ValueKind == JsonValueKind.Object && !saves.TryGetProperty(CorePlayerSave.SaveId, out _)));

    private void WritePlayer(Utf8JsonWriter writer, JsonElement player)
    {
        if (!NeedsSnapshot(player))
        {
            player.WriteTo(writer);
            return;
        }

        writer.WriteStartObject();
        foreach (var property in player.EnumerateObject())
        {
            if (!property.NameEquals(CorePlayerSave.SaveDictionary))
                property.WriteTo(writer);
        }

        writer.WriteStartObject(CorePlayerSave.SaveDictionary);
        if (player.TryGetProperty(CorePlayerSave.SaveDictionary, out var saves) && saves.ValueKind == JsonValueKind.Object)
        {
            foreach (var property in saves.EnumerateObject())
                property.WriteTo(writer);
        }
        writer.WriteString(CorePlayerSave.SaveId, _snapshots[player.GetProperty("net_id").GetUInt64()]);
        writer.WriteEndObject();
        writer.WriteEndObject();
    }

    public void Dispose() => Current.Value = _parent;
}
