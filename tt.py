#!/usr/bin/env python3
"""tt — trading tool for Claude's paper stock-trading test on Alpaca.

Claude makes the decisions; this tool supplies data, enforces risk rules in code,
places bracket orders, and keeps the journal. Every order requires a written thesis.

  python3 tt.py status                       account, positions, open orders, limits
  python3 tt.py scan                         liquid movers / most-actives worth a look
  python3 tt.py bars SYM [--tf day|hour|5min] [--n 40]
  python3 tt.py news [SYM ...] [--n 8]
  python3 tt.py enter SYM long|short --stop X --target Y --horizon swing|intraday --thesis "..." [--dry-run]
  python3 tt.py close SYM --reason "..."
  python3 tt.py flatten-intraday
  python3 tt.py note "free-text run summary"
  python3 tt.py score                        performance vs QQQ since start
Needs ALPACA_KEY / ALPACA_SECRET in the environment (paper keys).
"""
import os, sys, json, math, argparse, datetime as dt
from zoneinfo import ZoneInfo
import pandas as pd

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (MarketOrderRequest, TakeProfitRequest, StopLossRequest,
                                     GetOrdersRequest, GetPortfolioHistoryRequest)
from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass, QueryOrderStatus
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.historical.news import NewsClient
from alpaca.data.historical.screener import ScreenerClient
from alpaca.data.requests import (StockBarsRequest, StockLatestTradeRequest, NewsRequest,
                                  MostActivesRequest, MarketMoversRequest)
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.data.enums import DataFeed, Adjustment

# ---------------- risk rules (enforced in code, not by judgment) ----------------
CAP = 2000.0                 # virtual account size
RISK_PER_TRADE = 0.02        # max loss at stop: 2% of virtual equity
MAX_POS_FRAC = 0.50          # max 50% of equity in one position
MAX_GROSS_FRAC = 1.00        # no leverage
MAX_POSITIONS = 4
MIN_PRICE = 5.0
MIN_DOLLAR_VOL = 20e6        # 20-day average dollar volume
MIN_RR = 1.5                 # target distance >= 1.5x stop distance
DAILY_LOSS_LIMIT = 0.05
KILL_SWITCH = 0.40
INTRADAY_LAST_ENTRY = dt.time(12, 0)    # Pacific
HERE = os.path.dirname(os.path.abspath(__file__))
STATE, JOURNAL = os.path.join(HERE, "state.json"), os.path.join(HERE, "journal.jsonl")
PT, ET = ZoneInfo("America/Los_Angeles"), ZoneInfo("America/New_York")

K, S = os.environ["ALPACA_KEY"], os.environ["ALPACA_SECRET"]
T = TradingClient(K, S, paper=True)
D = StockHistoricalDataClient(K, S)


def now_pt():
    return dt.datetime.now(PT)


def jlog(rec):
    rec = {"ts": now_pt().isoformat(timespec="seconds"), **rec}
    with open(JOURNAL, "a") as f:
        f.write(json.dumps(rec) + "\n")


def state():
    if os.path.exists(STATE):
        return json.load(open(STATE))
    a = T.get_account()
    s = {"baseline_account_equity": float(a.equity), "start_date": str(dt.date.today()),
         "killed": False, "intraday": []}
    json.dump(s, open(STATE, "w"), indent=1)
    return s


def save(s):
    json.dump(s, open(STATE, "w"), indent=1)


def equity(s, a=None):
    a = a or T.get_account()
    off = CAP - s["baseline_account_equity"]
    return float(a.equity) + off, float(a.last_equity) + off


def last_price(sym):
    return float(D.get_stock_latest_trade(StockLatestTradeRequest(symbol_or_symbols=sym, feed=DataFeed.IEX))[sym].price)


def daily(sym, n=60):
    end = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=20)
    r = StockBarsRequest(symbol_or_symbols=sym, timeframe=TimeFrame.Day, start=end - dt.timedelta(days=n * 1.6 + 10),
                         end=end, feed=DataFeed.SIP, adjustment=Adjustment.ALL)
    df = D.get_stock_bars(r).df
    return df.xs(sym).tail(n) if len(df) else df


# ---------------- commands ----------------
def cmd_status(a):
    s = state()
    acct = T.get_account()
    eq, last = equity(s, acct)
    print(f"Virtual equity ${eq:,.2f}  (start ${CAP:,.0f}, {eq/CAP-1:+.2%})   today {eq-last:+.2f} ({eq/last-1:+.2%})")
    print(f"Limits: daily stop at ${last*(1-DAILY_LOSS_LIMIT):,.2f}, kill switch at ${CAP*(1-KILL_SWITCH):,.2f}"
          f"{'   ** KILLED **' if s['killed'] else ''}")
    clock = T.get_clock()
    print(f"Market {'OPEN' if clock.is_open else 'closed'}; now {now_pt():%a %H:%M} PT; next close "
          f"{clock.next_close.astimezone(PT):%a %H:%M} PT")
    pos = T.get_all_positions()
    if pos:
        print("Positions:")
        for p in pos:
            tag = "intraday" if p.symbol in s["intraday"] else "swing"
            print(f"  {p.symbol:6s} {p.side.value:5s} qty {p.qty:>6s} avg {float(p.avg_entry_price):9.2f} "
                  f"last {float(p.current_price):9.2f}  P&L {float(p.unrealized_pl):+8.2f} ({float(p.unrealized_plpc):+.2%}) [{tag}]")
    else:
        print("Positions: none")
    oo = T.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, nested=True))
    for o in oo:
        legs = " ".join(f"{l.type.value}@{l.stop_price or l.limit_price}" for l in (o.legs or []))
        print(f"  open order {o.symbol} {o.side.value} {o.qty} {o.type.value} {o.status.value} {legs}")


def cmd_scan(a):
    sc = ScreenerClient(K, S)
    syms = set()
    try:
        syms |= {x.symbol for x in sc.get_most_actives(MostActivesRequest(top=50)).most_actives}
        mv = sc.get_market_movers(MarketMoversRequest(top=50))
        syms |= {x.symbol for x in mv.gainers + mv.losers}
    except Exception as e:
        print("screener error:", e)
    core = ("AAPL MSFT NVDA AMZN GOOGL META TSLA AVGO AMD NFLX CRM ORCL ADBE INTC MU QCOM "
            "JPM BAC GS MS WFC V MA PYPL COIN XOM CVX OXY UNH LLY JNJ PFE MRK ABBV "
            "WMT COST HD NKE MCD SBUX DIS BA CAT GE UBER SHOP PLTR SNOW CRWD PANW SMCI ARM").split()
    syms |= set(core)
    syms = sorted(x for x in syms if x.isalpha() and len(x) <= 5)
    end = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=20)
    r = StockBarsRequest(symbol_or_symbols=syms, timeframe=TimeFrame.Day, start=end - dt.timedelta(days=420),
                         end=end, feed=DataFeed.SIP, adjustment=Adjustment.ALL)
    df = D.get_stock_bars(r).df.reset_index()
    rows = []
    for sym, g in df.groupby("symbol"):
        if len(g) < 60:
            continue
        c, v = g.close.values, g.volume.values
        dvol = (c[-20:] * v[-20:]).mean()
        if c[-1] < MIN_PRICE or dvol < MIN_DOLLAR_VOL:
            continue
        d = pd.Series(c).diff()
        up, dn = d.clip(lower=0).ewm(alpha=1 / 14).mean(), (-d.clip(upper=0)).ewm(alpha=1 / 14).mean()
        rsi14 = float((100 - 100 / (1 + up / dn)).iloc[-1])
        rows.append(dict(sym=sym, close=round(c[-1], 2), chg1d=c[-1] / c[-2] - 1, chg5d=c[-1] / c[-6] - 1,
                         chg20d=c[-1] / c[-21] - 1, vs52wHi=c[-1] / c[-252:].max() - 1,
                         relvol=v[-1] / v[-21:-1].mean(), rsi14=round(rsi14), dvol_m=round(dvol / 1e6)))
    t = pd.DataFrame(rows)
    if t.empty:
        print("nothing passed filters"); return
    pd.set_option("display.width", 200)
    fmt = lambda x: x.assign(**{k: (x[k] * 100).round(1) for k in ["chg1d", "chg5d", "chg20d", "vs52wHi"]},
                             relvol=x.relvol.round(1))
    print(f"{len(t)} liquid names (price>=${MIN_PRICE:.0f}, $vol>=${MIN_DOLLAR_VOL/1e6:.0f}M). % columns in percent.")
    print("\nUnusual volume (relvol>=1.8):\n" + fmt(t[t.relvol >= 1.8].sort_values("relvol", ascending=False).head(12)).to_string(index=False))
    print("\nBiggest 1-day moves:\n" + fmt(t.reindex(t.chg1d.abs().sort_values(ascending=False).index).head(12)).to_string(index=False))
    print("\nNear 52-week highs (within 3%):\n" + fmt(t[t.vs52wHi > -0.03].sort_values("chg20d", ascending=False).head(12)).to_string(index=False))
    print("\nOversold (RSI14<30):\n" + fmt(t[t.rsi14 < 30].sort_values("rsi14").head(10)).to_string(index=False))


def cmd_bars(a):
    tf = {"day": TimeFrame.Day, "hour": TimeFrame.Hour, "5min": TimeFrame(5, TimeFrameUnit.Minute)}[a.tf]
    end = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=16)
    span = {"day": a.n * 1.6 + 10, "hour": a.n / 6 + 5, "5min": a.n / 70 + 4}[a.tf]
    feed = DataFeed.SIP
    r = StockBarsRequest(symbol_or_symbols=a.sym, timeframe=tf, start=end - dt.timedelta(days=span), end=end,
                         feed=feed, adjustment=Adjustment.ALL)
    df = D.get_stock_bars(r).df.xs(a.sym).tail(a.n)
    if a.tf != "day":   # append real-time IEX bars for the last 16 minutes
        try:
            r2 = StockBarsRequest(symbol_or_symbols=a.sym, timeframe=tf, start=end, feed=DataFeed.IEX)
            df = pd.concat([df, D.get_stock_bars(r2).df.xs(a.sym)]).tail(a.n)
        except Exception:
            pass
    df.index = df.index.tz_convert(ET).strftime("%m-%d %H:%M" if a.tf != "day" else "%Y-%m-%d")
    print(df[["open", "high", "low", "close", "volume"]].round(2).to_string())
    try:
        print(f"last trade (IEX real-time): {last_price(a.sym):.2f}")
    except Exception:
        pass


def cmd_news(a):
    req = NewsRequest(symbols=",".join(a.syms) if a.syms else None, limit=a.n,
                      start=dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=4))
    for n in NewsClient(K, S).get_news(req).data["news"]:
        print(f"{n.created_at.astimezone(PT):%m-%d %H:%M} [{','.join(n.symbols[:4])}] {n.headline}")
        if a.full and n.summary:
            print("    " + n.summary[:300])


def check_kill(s, eq, last):
    if eq <= CAP * (1 - KILL_SWITCH):
        if not s["killed"]:
            T.close_all_positions(cancel_orders=True); s["killed"] = True; save(s)
            jlog({"type": "kill_switch", "equity": eq})
        return "KILL SWITCH tripped — trading stopped permanently. Review with Conor."
    if eq <= last * (1 - DAILY_LOSS_LIMIT):
        return f"Daily loss limit hit (equity {eq:.2f} vs {last:.2f}). No new entries today."
    return None


def cmd_enter(a):
    s = state()
    acct = T.get_account()
    eq, last = equity(s, acct)
    if (msg := check_kill(s, eq, last)):
        sys.exit("REJECTED: " + msg)
    if not a.thesis or len(a.thesis) < 40:
        sys.exit("REJECTED: thesis must be a real sentence or two (>=40 chars).")
    if not T.get_clock().is_open and not a.dry_run:
        sys.exit("REJECTED: market closed.")
    if a.horizon == "intraday" and now_pt().time() > INTRADAY_LAST_ENTRY:
        sys.exit("REJECTED: no new intraday entries after 12:00 PT.")
    pos = T.get_all_positions()
    if any(p.symbol == a.sym for p in pos):
        sys.exit("REJECTED: already have a position in " + a.sym)
    if len(pos) >= MAX_POSITIONS:
        sys.exit(f"REJECTED: max {MAX_POSITIONS} positions.")
    asset = T.get_asset(a.sym)
    if not asset.tradable:
        sys.exit("REJECTED: not tradable.")
    if a.side == "short" and not (asset.shortable and asset.easy_to_borrow):
        sys.exit("REJECTED: not easy to borrow for shorting.")
    d = daily(a.sym, 25)
    dvol = float((d.close * d.volume).tail(20).mean())
    px = last_price(a.sym)
    if px < MIN_PRICE or dvol < MIN_DOLLAR_VOL:
        sys.exit(f"REJECTED: liquidity/price filter (px {px:.2f}, $vol {dvol/1e6:.0f}M).")
    sgn = 1 if a.side == "long" else -1
    risk_ps, reward_ps = (px - a.stop) * sgn, (a.target - px) * sgn
    if risk_ps <= 0 or reward_ps <= 0:
        sys.exit(f"REJECTED: stop/target on wrong side of price {px:.2f}.")
    if reward_ps / risk_ps < MIN_RR:
        sys.exit(f"REJECTED: reward/risk {reward_ps/risk_ps:.2f} < {MIN_RR}.")
    gross = sum(abs(float(p.market_value)) for p in pos)
    qty = min(math.floor(eq * RISK_PER_TRADE / risk_ps), math.floor(eq * MAX_POS_FRAC / px),
              math.floor(max(0, eq * MAX_GROSS_FRAC - gross) / px))
    if qty < 1:
        sys.exit(f"REJECTED: size rounds to 0 shares (px {px:.2f}, risk/sh {risk_ps:.2f}).")
    plan = dict(symbol=a.sym, side=a.side, qty=qty, ref_price=round(px, 2), stop=a.stop, target=a.target,
                risk_usd=round(qty * risk_ps, 2), notional=round(qty * px, 2), rr=round(reward_ps / risk_ps, 2),
                horizon=a.horizon, thesis=a.thesis)
    print(json.dumps(plan, indent=1))
    if a.dry_run:
        print("(dry run — nothing sent)"); return
    req = MarketOrderRequest(symbol=a.sym, qty=qty, side=OrderSide.BUY if sgn > 0 else OrderSide.SELL,
                             time_in_force=TimeInForce.DAY if a.horizon == "intraday" else TimeInForce.GTC,
                             order_class=OrderClass.BRACKET, take_profit=TakeProfitRequest(limit_price=round(a.target, 2)),
                             stop_loss=StopLossRequest(stop_price=round(a.stop, 2)))
    o = T.submit_order(req)
    plan["order_id"] = str(o.id)
    jlog({"type": "entry", **plan})
    if a.horizon == "intraday":
        s["intraday"] = sorted(set(s["intraday"]) | {a.sym}); save(s)
    print("SENT", o.id, o.status.value)


def cmd_close(a):
    s = state()
    p = next((p for p in T.get_all_positions() if p.symbol == a.sym), None)
    if not p:
        sys.exit("No position in " + a.sym)
    for o in T.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[a.sym])):
        T.cancel_order_by_id(o.id)
    T.close_position(a.sym)
    jlog({"type": "exit", "symbol": a.sym, "qty": p.qty, "exit_ref": float(p.current_price),
          "unrealized_pl": float(p.unrealized_pl), "reason": a.reason})
    s["intraday"] = [x for x in s["intraday"] if x != a.sym]; save(s)
    print(f"Closed {a.sym}: P&L ~{float(p.unrealized_pl):+.2f}")


def cmd_flatten(a):
    s = state()
    held = {p.symbol: p for p in T.get_all_positions()}
    for sym in list(s["intraday"]):
        if sym in held:
            a.sym, a.reason = sym, "end-of-day flatten (intraday trade)"
            cmd_close(a)
    s = state(); s["intraday"] = []; save(s)
    print("Intraday positions flat.")


def cmd_note(a):
    jlog({"type": "note", "text": a.text}); print("noted")


def cmd_score(a):
    s = state()
    start = pd.Timestamp(s["start_date"])
    h = T.get_portfolio_history(GetPortfolioHistoryRequest(timeframe="1D", date_start=start.date()))
    eqs = pd.Series(h.equity, index=pd.to_datetime(h.timestamp, unit="s")).dropna()
    eqs = eqs + (CAP - s["baseline_account_equity"])
    eqs.index = eqs.index.normalize()
    q = daily("QQQ", 400)
    q.index = q.index.tz_convert(ET).tz_localize(None).normalize()
    q = q.close[q.index >= start]
    cur, _ = equity(s)
    qret = q.iloc[-1] / q.iloc[0] - 1 if len(q) > 1 else 0.0
    ent = [json.loads(l) for l in open(JOURNAL)] if os.path.exists(JOURNAL) else []
    n_entries = sum(r["type"] == "entry" for r in ent)
    closed = [o for o in T.get_orders(GetOrdersRequest(status=QueryOrderStatus.CLOSED, limit=500,
                                                        after=start.tz_localize(ET))) if o.filled_at]
    print(f"Since {s['start_date']}: bot ${cur:,.2f} ({cur/CAP-1:+.2%})  vs QQQ {qret:+.2%}  -> "
          f"{'AHEAD' if cur/CAP-1 > qret else 'BEHIND'} by {abs(cur/CAP-1-qret):.2%}")
    print(f"Entries: {n_entries}; filled orders (incl. exits): {len(closed)}; max drawdown "
          f"{(eqs/eqs.cummax()-1).min():.2%}" if len(eqs) else "")


def main():
    p = argparse.ArgumentParser(); sp = p.add_subparsers(dest="cmd", required=True)
    sp.add_parser("status"); sp.add_parser("scan"); sp.add_parser("flatten-intraday"); sp.add_parser("score")
    b = sp.add_parser("bars"); b.add_argument("sym"); b.add_argument("--tf", default="day", choices=["day", "hour", "5min"]); b.add_argument("--n", type=int, default=40)
    n = sp.add_parser("news"); n.add_argument("syms", nargs="*"); n.add_argument("--n", type=int, default=8); n.add_argument("--full", action="store_true")
    e = sp.add_parser("enter"); e.add_argument("sym"); e.add_argument("side", choices=["long", "short"])
    e.add_argument("--stop", type=float, required=True); e.add_argument("--target", type=float, required=True)
    e.add_argument("--horizon", choices=["swing", "intraday"], required=True); e.add_argument("--thesis", required=True)
    e.add_argument("--dry-run", action="store_true")
    c = sp.add_parser("close"); c.add_argument("sym"); c.add_argument("--reason", required=True)
    nt = sp.add_parser("note"); nt.add_argument("text")
    a = p.parse_args()
    a.sym = getattr(a, "sym", "").upper() if getattr(a, "sym", None) else None
    {"status": cmd_status, "scan": cmd_scan, "bars": cmd_bars, "news": cmd_news, "enter": cmd_enter,
     "close": cmd_close, "flatten-intraday": cmd_flatten, "note": cmd_note, "score": cmd_score}[a.cmd](a)


if __name__ == "__main__":
    main()
