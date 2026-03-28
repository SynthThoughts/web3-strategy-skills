import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import type { Position, PositionLeg } from "@/types/dashboard";
import {
  formatUSD,
  formatPct,
  formatRate,
  formatNum,
  exchangeName,
} from "@/lib/format";

interface Props {
  position: Position | null;
}

/** Color helper for a cell value */
function cellColor(color?: "profit" | "loss" | "warn" | "muted") {
  if (color === "profit") return "text-profit";
  if (color === "loss") return "text-loss";
  if (color === "warn") return "text-yellow-400";
  if (color === "muted") return "text-muted-foreground";
  return "text-foreground";
}

/** A single row in the 3-col comparison: leftVal | label | rightVal */
function CompareRow({
  label,
  leftVal,
  rightVal,
  leftColor,
  rightColor,
}: {
  label: string;
  leftVal: string;
  rightVal: string;
  leftColor?: "profit" | "loss" | "warn" | "muted";
  rightColor?: "profit" | "loss" | "warn" | "muted";
}) {
  return (
    <div className="grid grid-cols-[110px_1fr_1fr] gap-x-2 items-center py-[3px]">
      <span className="text-xs text-muted-foreground whitespace-nowrap">
        {label}
      </span>
      <span className={`text-xs font-mono font-medium text-right ${cellColor(leftColor)}`}>
        {leftVal}
      </span>
      <span className={`text-xs font-mono font-medium text-right ${cellColor(rightColor)}`}>
        {rightVal}
      </span>
    </div>
  );
}

/** Section divider in comparison table */
function CompareDivider({ label }: { label: string }) {
  return (
    <div className="mt-2 pt-2 border-t border-border/30">
      <span className="text-[10px] text-muted-foreground font-semibold uppercase tracking-wider">
        {label}
      </span>
    </div>
  );
}

function formatSettleCountdown(min: number) {
  return min >= 60 ? `${Math.floor(min / 60)}h ${min % 60}m` : `${min}m`;
}

function fundingRateColor(leg: PositionLeg) {
  const isLong = leg.side === "long";
  // positive rate: longs pay shorts. Negative rate: shorts pay longs.
  return leg.funding_rate >= 0
    ? isLong ? "loss" as const : "profit" as const
    : isLong ? "profit" as const : "loss" as const;
}

function pnlColor(v: number) {
  return v >= 0 ? "profit" as const : "loss" as const;
}

function signUSD(v: number) {
  const rounded = Math.round(v * 100) / 100;
  return `${rounded >= 0 ? "+" : ""}${formatUSD(rounded)}`;
}

function NoPosition() {
  return (
    <Card className="bg-[#1a2235] border border-[rgba(255,255,255,0.06)] rounded-xl">
      <CardHeader>
        <CardTitle className="text-[14px] font-semibold text-white">Current Position</CardTitle>
      </CardHeader>
      <CardContent>
        <div className="flex flex-col items-center justify-center py-8 text-muted-foreground">
          <div className="text-4xl mb-2 opacity-30">---</div>
          <p className="text-sm">No active position</p>
        </div>
      </CardContent>
    </Card>
  );
}

export function PositionDetail({ position }: Props) {
  if (!position || !position.has_position) return <NoPosition />;

  const { long_leg, short_leg } = position;
  const totalFunding = position.total_funding_pnl + (position.total_pending_funding ?? 0);
  const totalPnlColor = totalFunding >= 0 ? "profit" : "loss";

  return (
    <Card className="bg-[#1a2235] border border-[rgba(255,255,255,0.06)] rounded-xl">
      <CardHeader className="flex flex-row items-center justify-between pb-4">
        <div className="flex items-center gap-3">
          <CardTitle className="text-[14px] font-semibold text-white">Current Position</CardTitle>
          <span className="text-2xl font-bold font-mono">{position.coin}</span>
        </div>
        <Badge
          variant="outline"
          className="border-profit/50 text-profit bg-profit/10 text-[10px]"
        >
          ACTIVE
        </Badge>
      </CardHeader>

      <CardContent>
        <div className="grid grid-cols-1 gap-3 lg:grid-cols-[5fr_3fr]">
          {/* Left: Comparison table */}
          <div className="rounded-lg border border-border/50 bg-secondary/20 px-4 py-3">
            {/* Headers */}
            <div className="grid grid-cols-[110px_1fr_1fr] gap-x-2 items-center pb-2 mb-2 border-b border-border/40">
              <span />
              <div className="flex items-center justify-end gap-2">
                <span className="text-sm font-semibold">
                  {exchangeName(long_leg.exchange)}
                </span>
                <Badge
                  variant="outline"
                  className="text-[10px] px-1.5 py-0 border-profit/40 text-profit"
                >
                  LONG
                </Badge>
              </div>
              <div className="flex items-center justify-end gap-1.5">
                <span className="text-sm font-semibold">
                  {exchangeName(short_leg.exchange)}
                </span>
                <Badge
                  variant="outline"
                  className="text-[10px] px-1.5 py-0 border-loss/40 text-loss"
                >
                  SHORT
                </Badge>
              </div>
            </div>

            {/* Position rows */}
            <CompareRow
              label="Leverage"
              leftVal={`${long_leg.leverage}x`}
              rightVal={`${short_leg.leverage}x`}
            />
            <CompareRow
              label="Price PnL"
              leftVal={signUSD(long_leg.unrealized_pnl)}
              rightVal={signUSD(short_leg.unrealized_pnl)}
              leftColor={pnlColor(long_leg.unrealized_pnl)}
              rightColor={pnlColor(short_leg.unrealized_pnl)}
            />
            <CompareRow
              label="Size"
              leftVal={`${formatNum(long_leg.size, 4)} ${position.coin}`}
              rightVal={`${formatNum(short_leg.size, 4)} ${position.coin}`}
            />
            <CompareRow
              label="Entry Price"
              leftVal={formatUSD(long_leg.entry_price)}
              rightVal={formatUSD(short_leg.entry_price)}
            />
            <CompareRow
              label="Mark Price"
              leftVal={formatUSD(long_leg.current_price)}
              rightVal={formatUSD(short_leg.current_price)}
            />
            <CompareRow
              label="Notional"
              leftVal={formatUSD(long_leg.notional)}
              rightVal={formatUSD(short_leg.notional)}
            />
            {/* Funding section */}
            <CompareDivider label="Funding" />
            <CompareRow
              label="Rate"
              leftVal={formatRate(long_leg.funding_rate)}
              rightVal={formatRate(short_leg.funding_rate)}
              leftColor={fundingRateColor(long_leg)}
              rightColor={fundingRateColor(short_leg)}
            />
            <CompareRow
              label="Settlement"
              leftVal={`Every ${long_leg.settlement_cycle_h}h`}
              rightVal={`Every ${short_leg.settlement_cycle_h}h`}
              leftColor="muted"
              rightColor="muted"
            />
            <CompareRow
              label="Next In"
              leftVal={formatSettleCountdown(long_leg.next_settlement_min)}
              rightVal={formatSettleCountdown(short_leg.next_settlement_min)}
              leftColor={long_leg.next_settlement_min <= 5 ? "warn" : "muted"}
              rightColor={short_leg.next_settlement_min <= 5 ? "warn" : "muted"}
            />
            <CompareRow
              label="Settled"
              leftVal={signUSD(long_leg.accumulated_funding)}
              rightVal={signUSD(short_leg.accumulated_funding)}
              leftColor={pnlColor(long_leg.accumulated_funding)}
              rightColor={pnlColor(short_leg.accumulated_funding)}
            />
            <CompareRow
              label="Pending"
              leftVal={signUSD(long_leg.pending_funding ?? 0)}
              rightVal={signUSD(short_leg.pending_funding ?? 0)}
              leftColor={pnlColor(long_leg.pending_funding ?? 0)}
              rightColor={pnlColor(short_leg.pending_funding ?? 0)}
            />
          </div>

          {/* Right: Aggregate panels stacked */}
          <div className="flex flex-col gap-3">
            {/* Spread & Yield */}
            <div className={`rounded-lg border px-4 py-3 ${
              position.current_apr >= 0
                ? "border-profit/20 bg-profit/[0.04]"
                : "border-loss/20 bg-loss/[0.04]"
            }`}>
              <p className="text-[11px] text-muted-foreground uppercase tracking-wider font-medium mb-1.5">Current APR</p>
              <p
                className={`text-3xl font-bold font-mono leading-tight ${
                  position.current_apr >= 0 ? "text-profit" : "text-loss"
                }`}
              >
                {formatPct(position.current_apr)}
              </p>
              <div className="mt-2 flex justify-between text-xs">
                <span className="text-muted-foreground">Current Spread</span>
                <span className="font-mono text-muted-foreground">{(position.current_spread * 100).toFixed(4)}%</span>
              </div>
              <div className="flex justify-between text-xs">
                <span className="text-muted-foreground">Entry Spread</span>
                <span className="font-mono text-muted-foreground">{(position.entry_spread * 100).toFixed(4)}%</span>
              </div>
              <div className="flex justify-between text-xs">
                <span className="text-muted-foreground">Est. Daily</span>
                <span className={`font-mono ${position.projected_daily_usd >= 0 ? "text-profit" : "text-loss"}`}>{formatUSD(position.projected_daily_usd)}/day</span>
              </div>
            </div>

            {/* Funding PnL */}
            <div className={`rounded-lg border px-4 py-3 ${
              totalPnlColor === "profit"
                ? "border-profit/20 bg-profit/[0.04]"
                : "border-loss/20 bg-loss/[0.04]"
            }`}>
              <p className="text-[11px] text-muted-foreground uppercase tracking-wider font-medium mb-1.5">Funding PnL</p>
              <p
                className={`text-3xl font-bold font-mono leading-tight ${
                  totalPnlColor === "profit" ? "text-profit" : "text-loss"
                }`}
              >
                {signUSD(totalFunding)}
              </p>
              <div className="mt-2 flex justify-between text-xs">
                <span className="text-muted-foreground">Settled</span>
                <span className="font-mono text-muted-foreground">{signUSD(position.total_funding_pnl)}</span>
              </div>
              <div className="flex justify-between text-xs">
                <span className="text-muted-foreground">Pending</span>
                <span className="font-mono text-yellow-400">{signUSD(position.total_pending_funding ?? 0)}</span>
              </div>
              <div className="flex justify-between text-xs">
                <span className="text-muted-foreground">Delta Exposure</span>
                <span className={`font-mono ${position.delta_neutral ? "text-profit" : "text-loss"}`}>
                  {formatPct(position.delta_exposure_pct)} ({position.delta_neutral ? "Neutral" : "Exposed"})
                </span>
              </div>
            </div>

          </div>
        </div>
      </CardContent>
    </Card>
  );
}
