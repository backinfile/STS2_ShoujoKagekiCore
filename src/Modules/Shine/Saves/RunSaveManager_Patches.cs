using System.Reflection;
using HarmonyLib;
using MegaCrit.Sts2.Core.Saves;
using MegaCrit.Sts2.Core.Saves.Managers;
using MegaCrit.Sts2.Core.Saves.Runs;
namespace ShoujoKageki.Core.Runtime.Modules.Shine.Saves;

/// <summary>Bridges Core player snapshots when the native JSON metadata lacks the BaseLib extension.</summary>
[HarmonyPatch]
internal static class RunSaveManager_Patches
{
    [HarmonyPatch(typeof(GodotFileIo), nameof(GodotFileIo.ReadFile)), HarmonyPostfix]
    private static void ReadFile(string path, string? __result)
    {
        if (path.Contains(RunSaveManager.runSaveFileName) || path.Contains(RunSaveManager.multiplayerRunSaveFileName))
            KarenRunSaveReadScope.Record(__result);
    }
    [HarmonyPatch(typeof(RunSaveManager), nameof(RunSaveManager.SaveRun), new[] { typeof(SerializableRun), typeof(bool) })]
    private static class NativeWrite
    {
        private static void Prefix(SerializableRun save, bool isMultiplayer, out CoreRunSaveWriteScope __state)
            => __state = new(save, isMultiplayer);

        private static Exception? Finalizer(Exception? __exception, CoreRunSaveWriteScope? __state)
        { __state?.Dispose(); return __exception; }
    }
    [HarmonyPatch]
    private static class WriteBytes
    {
        private static IEnumerable<MethodBase> TargetMethods()
        {
            Type[] parameters = [typeof(string), typeof(byte[])];
            foreach (var store in new[] { typeof(GodotFileIo), typeof(CloudSaveStore) })
            {
                yield return AccessTools.Method(store, nameof(GodotFileIo.WriteFile), parameters);
                yield return AccessTools.Method(store, nameof(GodotFileIo.WriteFileAsync), parameters);
            }
        }

        private static void Prefix(string path, ref byte[] bytes)
            => bytes = CoreRunSaveWriteScope.IncludeSnapshots(path, bytes);
    }
    [HarmonyPatch]
    private static class NativeRead
    {
        private static IEnumerable<MethodBase> TargetMethods()
        {
            yield return AccessTools.Method(typeof(RunSaveManager), nameof(RunSaveManager.LoadRunSave));
            yield return AccessTools.Method(typeof(RunSaveManager), nameof(RunSaveManager.LoadMultiplayerRunSave));
        }
        private static void Prefix(out KarenRunSaveReadScope __state) => __state = new();
        private static void Postfix(ReadSaveResult<SerializableRun> __result, KarenRunSaveReadScope __state) => __state.Bind(__result);
        private static Exception? Finalizer(Exception? __exception, KarenRunSaveReadScope? __state)
        { __state?.Dispose(); return __exception; }
    }
}
