#!/usr/bin/env node
/*
 * Generate known-good SWV-ZF2 payloads from Zigbee2MQTT's own converters.
 *
 * The quirk's plan encoder, rain-delay encoder and Time-cluster responder are
 * ports of zigbee-herdsman-converters (src/devices/sonoff.ts) and
 * zigbee-herdsman (src/utils/timeService.ts). This script drives the REAL Z2M
 * code with mocked endpoints/clocks and records what it would put on the air;
 * tests/test_schedules_z2m_vectors.py then checks the Python port produces the
 * same bytes. The output is committed (tests/fixtures/z2m_vectors.json) so CI
 * needs no Node.
 *
 * Usage (outside the repo, e.g. a scratch dir):
 *   npm install --ignore-scripts zigbee-herdsman-converters@26.117.0
 *   node gen_vectors.cjs <that dir>/node_modules > tests/fixtures/z2m_vectors.json
 */
"use strict";

const path = require("node:path");
const {spawnSync} = require("node:child_process");

const nodeModules = path.resolve(process.argv[2] || "node_modules");
const zhcDist = path.join(nodeModules, "zigbee-herdsman-converters", "dist");
const timeServicePath = path.join(nodeModules, "zigbee-herdsman", "dist", "utils", "timeService.js");

// ── Clock mocking (Time-cluster and rain-delay vectors depend on "now") ──
const RealDate = Date;
function freezeNow(ms) {
    global.Date = class extends RealDate {
        constructor(...args) {
            if (args.length === 0) super(ms);
            else super(...args);
        }
        static now() {
            return ms;
        }
    };
}

// ── Child mode: one Time / rain-delay case under a given TZ (set by parent) ──
if (process.argv[3] === "--child") {
    const job = JSON.parse(process.argv[4]);
    freezeNow(RealDate.parse(job.now));
    if (job.kind === "time") {
        const ts = require(timeServicePath);
        ts.clearCachedTimeData();
        const a = ts.getTimeClusterAttributes();
        process.stdout.write(
            JSON.stringify({
                time: a.time,
                time_zone: a.timeZone,
                local_time: a.localTime,
                dst_shift: a.dstShift,
                // swvznGenTimeCompatResponse overrides these two with 0.
                dst_start: 0,
                dst_end: 0,
            }),
        );
    } else if (job.kind === "rain_delay") {
        const def = loadDefinition();
        const conv = def.toZigbee.find((c) => c.key.includes("rain_delay"));
        const ep = mockEndpoint(1);
        Promise.resolve(conv.convertSet(ep, "rain_delay", job.value, mockMeta(def, undefined))).then(() => {
            process.stdout.write(JSON.stringify({endpoint_id: ep.ID, command: ep.sent[0].command, payload_hex: ep.sent[0].hex}));
        });
    }
    return;
}

function loadDefinition() {
    const zhc = require(path.join(zhcDist, "index.js"));
    const sonoff = require(path.join(zhcDist, "devices", "sonoff.js"));
    const raw = sonoff.definitions.find((d) => d.model === "SWV-ZF2");
    return zhc.prepareDefinition(raw);
}

function mockEndpoint(id) {
    return {
        ID: id,
        sent: [],
        async command(cluster, command, payload, options) {
            this.sent.push({cluster, command, hex: Buffer.from(payload.data).toString("hex"), options});
        },
    };
}

function mockMeta(def, endpointName) {
    return {
        device: {modelID: "SWV-ZF2", softwareBuildID: "1.0.9", ieeeAddr: "0xa4c138413009dea53", endpoints: []},
        mapped: def,
        state: {},
        options: {},
        message: {},
        endpoint_name: endpointName,
    };
}

// ── Plan vectors (deterministic: no clock or TZ involved) ──
const PLAN_CASES = [
    // The real program: daily, 5:00, front lawn 11 min, line A slot 0.
    {name: "daily_lineA_0500_11min", endpoint_name: "1", value: {plan_index: 0, enable_state: true, loop_type_mode: "weekdays", loop_type_week_days: {sunday: true, monday: true, tuesday: true, wednesday: true, thursday: true, friday: true, saturday: true}, enable_date: "2026-10-22", start_time: "05:00", irrigation_mode: "duration", irrigation_total_duration: 11, irrigation_amount_unit: "liter", create_datetime: "2026-10-22T09:30:00-07:00"}},
    {name: "daily_lineA_disabled", endpoint_name: "1", value: {plan_index: 0, enable_state: false, loop_type_mode: "weekdays", loop_type_week_days: {sunday: true, monday: true, tuesday: true, wednesday: true, thursday: true, friday: true, saturday: true}, enable_date: "2026-10-22", start_time: "05:00", irrigation_mode: "duration", irrigation_total_duration: 11, irrigation_amount_unit: "liter", create_datetime: "2026-10-22T09:30:00-07:00"}},
    {name: "daily_lineB_0512", endpoint_name: "2", value: {plan_index: 0, enable_state: true, loop_type_mode: "weekdays", loop_type_week_days: {sunday: true, monday: true, tuesday: true, wednesday: true, thursday: true, friday: true, saturday: true}, enable_date: "2026-10-22", start_time: "05:12", irrigation_mode: "duration", irrigation_total_duration: 11, irrigation_amount_unit: "liter", create_datetime: "2026-10-22T16:30:00Z"}},
    {name: "mon_wed_fri_index3", endpoint_name: "1", value: {plan_index: 3, enable_state: true, loop_type_mode: "weekdays", loop_type_week_days: {monday: true, wednesday: true, friday: true}, enable_date: "2026-11-02", start_time: "18:45", irrigation_mode: "duration", irrigation_total_duration: 20, irrigation_amount_unit: "liter", create_datetime: "2026-10-30T12:00:00+00:00"}},
    {name: "odd_days", endpoint_name: "1", value: {plan_index: 1, enable_state: true, loop_type_mode: "odd_days", enable_date: "2026-12-15", start_time: "06:30", irrigation_mode: "duration", irrigation_total_duration: 5, irrigation_amount_unit: "liter", create_datetime: "2026-12-14T23:59:59-08:00"}},
    {name: "even_days_lineB", endpoint_name: "2", value: {plan_index: 2, enable_state: true, loop_type_mode: "even_days", enable_date: "2027-03-14", start_time: "00:00", irrigation_mode: "duration", irrigation_total_duration: 1, irrigation_amount_unit: "liter", create_datetime: "2027-03-14T10:00:00-07:00"}},
    {name: "every_3_days", endpoint_name: "1", value: {plan_index: 4, enable_state: true, loop_type_mode: "day_interval", loop_type_interval_days: 3, enable_date: "2026-10-25", start_time: "23:59", irrigation_mode: "duration", irrigation_total_duration: 719, irrigation_amount_unit: "liter", create_datetime: "2026-10-22T00:00:00Z"}},
    {name: "every_30_days_index5", endpoint_name: "2", value: {plan_index: 5, enable_state: true, loop_type_mode: "day_interval", loop_type_interval_days: 30, enable_date: "2026-01-01", start_time: "12:00", irrigation_mode: "duration", irrigation_total_duration: 0, irrigation_amount_unit: "liter", create_datetime: "2026-01-01T00:00:00Z"}},
    {name: "capacity_50L_failsafe15", endpoint_name: "1", value: {plan_index: 1, enable_state: true, loop_type_mode: "weekdays", loop_type_week_days: {saturday: true, sunday: true}, enable_date: "2026-10-24", start_time: "07:15", irrigation_mode: "capacity", irrigation_amount: 50, fail_safe: 15, irrigation_amount_unit: "liter", create_datetime: "2026-10-22T09:30:00-07:00"}},
    {name: "capacity_us_gallon", endpoint_name: "2", value: {plan_index: 0, enable_state: true, loop_type_mode: "odd_days", enable_date: "2026-10-23", start_time: "04:00", irrigation_mode: "capacity", irrigation_amount: 10000, fail_safe: 719, irrigation_amount_unit: "us_gallon", create_datetime: "2026-10-22T09:30:00-07:00"}},
    {name: "duration_with_interval", endpoint_name: "1", value: {plan_index: 2, enable_state: true, loop_type_mode: "weekdays", loop_type_week_days: {tuesday: true, thursday: true}, enable_date: "2026-10-27", start_time: "05:30", irrigation_mode: "duration_with_interval", irrigation_total_duration: 20, irrigation_duration: 5, interval_duration: 3, irrigation_amount_unit: "liter", create_datetime: "2026-10-22T09:30:00-07:00"}},
    {name: "explicit_unused_fields", endpoint_name: "1", value: {plan_index: 0, enable_state: true, loop_type_mode: "weekdays", loop_type_week_days: {sunday: true}, enable_date: "2026-10-25", start_time: "05:00", irrigation_mode: "duration", irrigation_total_duration: 9, irrigation_duration: 60, interval_duration: 1, irrigation_amount: 1, fail_safe: 0, irrigation_amount_unit: "liter", create_datetime: "2026-10-22T09:30:00-07:00"}},
];

// ── Time vectors: TZ x instant, including both sides of the Nov 1 2026 DST end ──
const TIME_CASES = [
    {tz: "America/Los_Angeles", now: "2026-10-22T16:41:22Z"},
    {tz: "America/Los_Angeles", now: "2026-11-01T08:59:59Z"},
    {tz: "America/Los_Angeles", now: "2026-11-01T09:00:00Z"},
    {tz: "America/Los_Angeles", now: "2026-12-25T12:00:00Z"},
    {tz: "America/Los_Angeles", now: "2027-03-14T10:00:00Z"},
    {tz: "Europe/Rome", now: "2026-07-01T10:00:00Z"},
    {tz: "Europe/Rome", now: "2026-01-15T10:00:00Z"},
    {tz: "Australia/Sydney", now: "2026-01-15T10:00:00Z"},
    {tz: "Australia/Sydney", now: "2026-07-15T10:00:00Z"},
    {tz: "Asia/Tokyo", now: "2026-10-22T16:41:22Z"},
    {tz: "UTC", now: "2026-10-22T16:41:22Z"},
];

// "now" is a couple of hours before the delay end, inside the same DST period:
// Z2M converts with the offset at NOW, the port with the offset at the delay
// end; both agree away from a DST transition.
const RAIN_DELAY_CASES = [
    {tz: "America/Los_Angeles", now: "2026-10-22T16:00:00Z", value: "2026-10-23T09:00:00-07:00"},
    {tz: "America/Los_Angeles", now: "2026-12-01T16:00:00Z", value: "2026-12-03T08:00:00-08:00"},
    {tz: "Europe/Rome", now: "2026-07-01T08:00:00Z", value: "2026-07-02T10:00:00Z"},
    {tz: "America/Los_Angeles", now: "2026-10-22T16:00:00Z", value: "0"},
];

function runChild(tz, job) {
    const out = spawnSync(process.execPath, [__filename, nodeModules, "--child", JSON.stringify(job)], {
        env: {...process.env, TZ: tz},
        encoding: "utf8",
    });
    if (out.status !== 0) throw new Error(`child failed (${tz} ${JSON.stringify(job)}): ${out.stderr}`);
    return JSON.parse(out.stdout);
}

async function main() {
    const def = loadDefinition();
    const planConv = def.toZigbee.find((c) => c.key.includes("irrigation_plan_settings"));
    const endpointMap = def.endpoint ? def.endpoint({}) : {};
    const plans = [];
    for (const c of PLAN_CASES) {
        const ep = mockEndpoint(endpointMap[c.endpoint_name]);
        await planConv.convertSet(ep, "irrigation_plan_settings", c.value, mockMeta(def, c.endpoint_name));
        if (ep.sent.length !== 1) throw new Error(`case ${c.name}: Z2M rejected the input`);
        plans.push({...c, endpoint_id: ep.ID, command: ep.sent[0].command, payload_hex: ep.sent[0].hex});
    }

    const removeConv = def.toZigbee.find((c) => c.key.includes("irrigation_plan_remove"));
    const removes = [];
    for (const [name, index] of [["1", 0], ["2", 5]]) {
        const ep = mockEndpoint(endpointMap[name]);
        await removeConv.convertSet(ep, "irrigation_plan_remove", {plan_index: index}, mockMeta(def, name));
        removes.push({endpoint_name: name, plan_index: index, endpoint_id: ep.ID, command: ep.sent[0].command, payload_hex: ep.sent[0].hex});
    }

    const time = TIME_CASES.map((c) => ({...c, expected: runChild(c.tz, {kind: "time", now: c.now})}));
    const rain_delay = RAIN_DELAY_CASES.map((c) => ({...c, expected: runChild(c.tz, {kind: "rain_delay", now: c.now, value: c.value})}));

    const pkg = require(path.join(nodeModules, "zigbee-herdsman-converters", "package.json"));
    const hpkg = require(path.join(nodeModules, "zigbee-herdsman", "package.json"));
    process.stdout.write(
        `${JSON.stringify(
            {
                generator: "tests/z2m_oracle/gen_vectors.cjs",
                "zigbee-herdsman-converters": pkg.version,
                "zigbee-herdsman": hpkg.version,
                endpoint_map: endpointMap,
                plans,
                removes,
                time,
                rain_delay,
            },
            null,
            2,
        )}\n`,
    );
}

main().catch((e) => {
    console.error(e);
    process.exit(1);
});
