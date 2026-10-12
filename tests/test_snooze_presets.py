from frontend_module_loader import module_path
import json
import os
import subprocess
import textwrap
import unittest


SNOOZE_JS = module_path("app_snooze.js")


def run_snooze_cases() -> dict:
    source = SNOOZE_JS.read_text(encoding="utf-8")
    js = textwrap.dedent(
        f"""
        const vm = require("vm");
        const source = {json.dumps(source)};
        const ctx = {{ window: {{}} }};
        vm.createContext(ctx);
        vm.runInContext(source, ctx);
        const snooze = ctx.window.CodoxearSnooze;
        const at = (...parts) => Date.UTC(...parts);
        const wednesdayAfternoon = at(2026, 0, 7, 15, 0, 0);
        const wednesdayEvening = at(2026, 0, 7, 19, 0, 0);
        const saturdayMorning = at(2026, 0, 10, 9, 0, 0);
        const saturdayAfternoon = at(2026, 0, 10, 15, 0, 0);
        const sunday = at(2026, 0, 11, 12, 0, 0);
        const mondayMorning = at(2026, 0, 5, 8, 0, 0);
        process.stdout.write(JSON.stringify({{
          wednesdayAfternoon: snooze.resolveSnoozePresets(wednesdayAfternoon),
          wednesdayEvening: snooze.resolveSnoozePresets(wednesdayEvening),
          saturdayMorning: snooze.resolveSnoozePresets(saturdayMorning),
          saturdayAfternoon: snooze.resolveSnoozePresets(saturdayAfternoon),
          sunday: snooze.resolveSnoozePresets(sunday),
          mondayMorning: snooze.resolveSnoozePresets(mondayMorning),
          eveningEpoch: snooze.snoozePresetEpoch("evening", wednesdayAfternoon),
          noneEpoch: snooze.snoozePresetEpoch("none", wednesdayAfternoon),
        }}));
        """
    )
    proc = subprocess.run(
        ["node", "-e", js],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={"PATH": os.environ.get("PATH", ""), "TZ": "UTC"},
    )
    return json.loads(proc.stdout)


def preset_by_mode(presets: list[dict], mode: str) -> dict:
    return next(preset for preset in presets if preset["mode"] == mode)


class TestSnoozePresets(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.result = run_snooze_cases()

    def test_wednesday_life_anchor_presets(self) -> None:
        presets = self.result["wednesdayAfternoon"]
        self.assertEqual(
            preset_by_mode(presets, "evening"),
            {"mode": "evening", "label": "This evening", "timeText": "6pm", "epoch": 1767808800},
        )
        self.assertEqual(
            preset_by_mode(presets, "tomorrow"),
            {"mode": "tomorrow", "label": "Tomorrow", "timeText": "9am", "epoch": 1767862800},
        )
        self.assertEqual(
            preset_by_mode(presets, "weekend"),
            {"mode": "weekend", "label": "This weekend", "timeText": "Sat 10am", "epoch": 1768039200},
        )
        self.assertEqual(
            preset_by_mode(presets, "nextWeek"),
            {"mode": "nextWeek", "label": "Next week", "timeText": "Mon 9am", "epoch": 1768208400},
        )

    def test_evening_rolls_after_today_anchor(self) -> None:
        evening = preset_by_mode(self.result["wednesdayEvening"], "evening")
        self.assertEqual(evening["label"], "Tomorrow evening")
        self.assertEqual(evening["epoch"], 1767895200)

    def test_weekend_rolls_only_after_saturday_anchor(self) -> None:
        saturday_morning = preset_by_mode(self.result["saturdayMorning"], "weekend")
        self.assertEqual(saturday_morning["label"], "This weekend")
        self.assertEqual(saturday_morning["epoch"], 1768039200)

        saturday_afternoon = preset_by_mode(self.result["saturdayAfternoon"], "weekend")
        self.assertEqual(saturday_afternoon["label"], "Next weekend")
        self.assertEqual(saturday_afternoon["epoch"], 1768644000)

        sunday = preset_by_mode(self.result["sunday"], "weekend")
        self.assertEqual(sunday["label"], "Next weekend")
        self.assertEqual(sunday["epoch"], 1768644000)

    def test_next_week_is_always_a_full_week_out(self) -> None:
        next_week = preset_by_mode(self.result["mondayMorning"], "nextWeek")
        self.assertEqual(next_week["epoch"], 1768208400)

    def test_epoch_lookup_matches_resolver_and_omits_non_presets(self) -> None:
        evening = preset_by_mode(self.result["wednesdayAfternoon"], "evening")
        self.assertEqual(self.result["eveningEpoch"], evening["epoch"])
        self.assertIsNone(self.result["noneEpoch"])


if __name__ == "__main__":
    unittest.main()
