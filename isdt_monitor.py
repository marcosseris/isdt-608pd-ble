"""Command-line ISDT 608PD monitor (see isdt_gui.py for the GUI).

Usage:
    python isdt_monitor.py                 # monitor and alert when done
    python isdt_monitor.py --email         # ...and email you (set up in the GUI)
    python isdt_monitor.py --phone         # ...and push to your phone via Home Assistant
    python isdt_monitor.py --test-alert    # check the toast + sound work
"""

import argparse
import asyncio
import time

import isdt
import notify
from estimator import ChargeEstimator
from ha_live import HALive


async def monitor(email: bool, phone: bool) -> None:
    est = ChargeEstimator()
    ha = HALive()  # live sensors in Home Assistant, if set up in the GUI
    alarm = notify.Alarm()
    last = None
    async for rt in isdt.stream(on_state=lambda s: print(f"\n{time.strftime('%H:%M:%S')} {s}")):
        est.add(rt)
        ha.update(rt, est.phase, est.remaining_s(), est.eta(), urgent=rt.status != last)
        rem = est.remaining_s() if rt.status == 1 else None
        cells = " ".join(f"{c.mv / 1000:.3f}" for c in rt.cells)
        print(f"\r{time.strftime('%H:%M:%S')} {rt.status_name:12} {rt.percent:3d}% "
              f"{rt.pack_mv / 1000:6.2f}V {rt.current_ma / 1000:5.2f}A {rt.capacity_mah:5d}mAh "
              f"elapsed {isdt.fmt_hms(rt.elapsed_s)} left {isdt.fmt_hms(rem)} {est.phase or ''} [{cells}]  ",
              end="", flush=True)
        if last == 1 and rt.status == 2:
            msg = f"Charged to {rt.pack_mv / 1000:.2f} V ({rt.capacity_mah} mAh). Swap the battery!"
            print(f"\n*** {msg} ***")
            notify.toast("Battery charged", msg)
            alarm.start()
            sends = []
            if email:
                sends.append(("email", notify.send_email, ("Battery charged - ISDT 608PD", msg)))
            if phone:
                sends.append(("phone notification", notify.send_homeassistant, ("Battery charged", msg)))
            for name, fn, fn_args in sends:
                try:
                    await asyncio.to_thread(fn, *fn_args)
                    print(f"{name} sent")
                except Exception as e:
                    print(f"{name} failed: {e}")
        last = rt.status


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--email", action="store_true", help="also send an email when done")
    ap.add_argument("--phone", action="store_true", help="also notify your phone via Home Assistant")
    ap.add_argument("--test-alert", action="store_true", help="fire the alert without a charger")
    args = ap.parse_args()
    if args.test_alert:
        notify.toast("Battery charged", "This is a test alert")
        a = notify.Alarm(timeout_s=5)
        a.start()
        a._thread.join()
        return
    try:
        asyncio.run(monitor(args.email, args.phone))
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
