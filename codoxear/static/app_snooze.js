function compactLocalTime(date) {
  const hours = date.getHours();
  const hour = hours % 12 || 12;
  const minutes = date.getMinutes();
  return `${hour}${minutes ? `:${String(minutes).padStart(2, "0")}` : ""}${hours < 12 ? "am" : "pm"}`;
}

function epochSeconds(date) {
  return Math.floor(date.getTime() / 1000);
}

function localDate(nowMs) {
  return new Date(nowMs);
}

function resolveSnoozePresets(nowMs) {
  const now = Number(nowMs);

  const evening = localDate(now);
  evening.setHours(18, 0, 0, 0);
  let eveningLabel = "This evening";
  if (evening.getTime() <= now) {
    evening.setDate(evening.getDate() + 1);
    eveningLabel = "Tomorrow evening";
  }

  const tomorrow = localDate(now);
  tomorrow.setDate(tomorrow.getDate() + 1);
  tomorrow.setHours(9, 0, 0, 0);

  const monday = localDate(now);
  monday.setHours(0, 0, 0, 0);
  const isoDay = (monday.getDay() + 6) % 7;
  monday.setDate(monday.getDate() - isoDay);

  const weekend = new Date(monday.getTime());
  weekend.setDate(weekend.getDate() + 5);
  weekend.setHours(10, 0, 0, 0);
  let weekendLabel = "This weekend";
  if (weekend.getTime() <= now) {
    weekend.setDate(weekend.getDate() + 7);
    weekendLabel = "Next weekend";
  }

  const nextWeek = new Date(monday.getTime());
  nextWeek.setDate(nextWeek.getDate() + 7);
  nextWeek.setHours(9, 0, 0, 0);

  return [
    { mode: "evening", label: eveningLabel, timeText: compactLocalTime(evening), epoch: epochSeconds(evening) },
    { mode: "tomorrow", label: "Tomorrow", timeText: compactLocalTime(tomorrow), epoch: epochSeconds(tomorrow) },
    { mode: "weekend", label: weekendLabel, timeText: `Sat ${compactLocalTime(weekend)}`, epoch: epochSeconds(weekend) },
    { mode: "nextWeek", label: "Next week", timeText: `Mon ${compactLocalTime(nextWeek)}`, epoch: epochSeconds(nextWeek) },
  ];
}

function snoozePresetEpoch(mode, nowMs) {
  const preset = resolveSnoozePresets(nowMs).find((candidate) => candidate.mode === mode);
  return preset ? preset.epoch : null;
}

export { resolveSnoozePresets, snoozePresetEpoch };
