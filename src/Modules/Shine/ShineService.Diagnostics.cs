using System.Runtime.CompilerServices;
using MegaCrit.Sts2.Core.Entities.Players;
using MegaCrit.Sts2.Core.Models;
using MegaCrit.Sts2.Core.Rooms;
using MegaCrit.Sts2.Core.Runs;
using MegaCrit.Sts2.Core.Saves;
using MegaCrit.Sts2.Core.Saves.Runs;
using ShoujoKageki.Core.V1;

namespace ShoujoKageki.Core.Runtime.Modules.Shine;

public sealed partial class ShineService
{
    private enum DeckObservation { Cleanup = 1, Copy = 2, Save = 4, Restore = 8 }

    private sealed class DeckTrace
    {
        public string? LastWrite;
        public int Reported;
    }

    private static readonly ConditionalWeakTable<CardModel, DeckTrace> DeckTraces = new();

    private static void RememberDeckWrite(CardModel card, string path, CardResolutionKind? kind = null,
        int? before = null, CardModel? combat = null, int? combatBefore = null)
    {
        try
        {
            if (!card.IsMutable || card.Owner is not { } owner || !owner.Deck.Cards.Contains(card)) return;
            DeckTraces.GetOrCreateValue(card).LastWrite =
                $"{path} kind={kind} before={before} combatBefore={combatBefore} floor={owner.RunState.TotalFloor} " +
                $"nestedBatch={CurrentBatch?.Parent != null} combat=[{DescribeCard(combat)}]";
        }
        catch (Exception) { } // Diagnostics must not interrupt card resolution.
    }

    private static void ForgetDeckWrite(CardModel card)
    {
        if (DeckTraces.TryGetValue(card, out var trace)) trace.LastWrite = null;
    }

    private static bool IsAbnormalDeckCard(CardModel card) =>
        card is CoreEmptyShell || Depleted.Get(card) || (Maximum.Get(card) >= 0 && Remaining.Get(card) <= 0);

    private static string DescribeCard(CardModel? card) => card == null ? "-" :
        $"{card.Id} remaining={Remaining.Get(card)} maximum={Maximum.Get(card)} pending={Pending.Get(card)} " +
        $"depleted={Depleted.Get(card)} temporary={Temporary.Get(card)} dupe={card.IsDupe} removed={card.HasBeenRemovedFromState}";

    private static void ReportRetainedDeckCard(CardModel card, DeckObservation phase, string path,
        CardModel? combat = null, Exception? failure = null, SavedBudget? saved = null, Player? owner = null)
    {
        try
        {
            if (!(saved?.IsAbnormal == true || IsAbnormalDeckCard(card))) return;
            owner ??= card.Owner;
            if (owner == null || !owner.Deck.Cards.Contains(card)) return;
            var trace = DeckTraces.GetOrCreateValue(card);
            if ((Interlocked.Or(ref trace.Reported, (int)phase) & (int)phase) != 0) return;
            var run = owner.RunState;
            string code = phase switch
            {
                DeckObservation.Cleanup => "deck.cleanup",
                DeckObservation.Copy => "deck.shell",
                DeckObservation.Save => "deck.checkpoint",
                _ => "deck.restore"
            };
            var encounter = (run.CurrentRoom as CombatRoom)?.Encounter.Id;
            CoreApi.Get<ICoreDiagnostics>().Report(DiagnosticSeverity.Error, "shine", code,
                $"{card.Id} path={path}\n" +
                $"character={owner.Character.Id} floor={run.TotalFloor} encounter={encounter} addedFloor={card.FloorAddedToDeck} " +
                $"upgrade={card.CurrentUpgradeLevel} archiveCount={ShineArchive.KnownCount(owner)}\n" +
                $"permanent=[{DescribeCard(card)}] combat=[{DescribeCard(combat)}]\n" +
                $"lastWrite=[{trace.LastWrite ?? "unknown"}] saved=[{saved?.ToString() ?? "-"}] " +
                $"failure={failure?.GetType().FullName ?? "-"}\n{failure?.StackTrace}");
        }
        catch (Exception) { } // Diagnostics must not interrupt resolution, saving or loading.
    }

    internal static void ObserveRunCheckpoint(SerializableRun save)
    {
        try
        {
            if (RunManager.Instance.DebugOnlyGetState() is not { } run) return;
            foreach (var seat in save.Players)
                if (run.Players.FirstOrDefault(player => player.NetId == seat.NetId) is { } player)
                    InspectDeck(player, seat, DeckObservation.Save, "save");
        }
        catch (Exception) { }
    }

    internal static void ObserveRestoredDeck(Player player, SerializablePlayer save, string path)
    {
        try { InspectDeck(player, save, DeckObservation.Restore, path); }
        catch (Exception) { }
    }

    private static void InspectDeck(Player player, SerializablePlayer save, DeckObservation phase, string path)
    {
        var deck = player.Deck.Cards;
        for (int i = 0; i < Math.Min(deck.Count, save.Deck.Count); i++)
        {
            var card = deck[i];
            var serialized = save.Deck[i];
            if (card.Id != serialized.Id || card.FloorAddedToDeck != serialized.FloorAddedToDeck) continue;
            var budget = SavedBudget.Read(serialized);
            if (phase == DeckObservation.Save && !budget.IsAbnormal && card is not CoreEmptyShell) continue;
            // Player.FromSerializable has populated the deck before the run binds card ownership.
            ReportRetainedDeckCard(card, phase, path, saved: budget, owner: player);
        }
    }

    private readonly record struct SavedBudget(int? Current, int? Maximum, int? LegacyCurrent, int? LegacyMaximum, bool Depleted)
    {
        internal bool IsAbnormal => Depleted || (Maximum >= 0 ? Current is null or <= 0 :
            Current >= 0 || (LegacyMaximum >= 0 && LegacyCurrent is null or <= 0));

        internal static SavedBudget Read(SerializableCard card)
        {
            var budget = new SavedBudget();
            if (card.Props?.ints is { } fields)
                foreach (var field in fields)
                    budget = field.name switch
                    {
                        "CardModel_core_shine_current" => budget with { Current = field.value },
                        "CardModel_core_shine_max" => budget with { Maximum = field.value },
                        "CardModel_karen_shine_current" => budget with { LegacyCurrent = field.value },
                        "CardModel_karen_shine_max" => budget with { LegacyMaximum = field.value },
                        _ => budget
                    };
            return budget with
            {
                Depleted = card.Props?.bools?.Any(field => field.name == "CardModel_core_shine_depleted" && field.value) == true
            };
        }

        public override string ToString() =>
            $"remaining={Current?.ToString() ?? "missing"} maximum={Maximum?.ToString() ?? "missing"} " +
            $"legacyRemaining={LegacyCurrent?.ToString() ?? "missing"} legacyMaximum={LegacyMaximum?.ToString() ?? "missing"} depleted={Depleted}";
    }
}
