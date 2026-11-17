import exec from "k6/execution";
import http from "k6/http";
import { Counter, Gauge, Trend } from "k6/metrics";

const catalog = JSON.parse(open("./catalog.json"));
const operation = catalog.operations.find((entry) => entry.id === __ENV.PERF_API_ID);
const testCase = operation?.cases.find((entry) => entry.id === __ENV.PERF_CASE_ID);
const suite = __ENV.PERF_SUITE;
const runId = __ENV.PERF_RUN_ID;
const backendSha = __ENV.PERF_BACKEND_SHA;
const baseUrl = __ENV.BASE_URL;
const summaryPath = __ENV.PERF_SUMMARY_PATH;

if (!testCase || !["smoke", "load", "stress"].includes(suite) ||
    (testCase.suites && !testCase.suites.includes(suite))) {
    throw new Error("Unknown operation, case, or suite combination");
}
if (!/^[A-Za-z0-9._-]{1,120}$/.test(runId ?? "") ||
    !/^[0-9a-f]{40}$/.test(backendSha ?? "") || !summaryPath) {
    throw new Error("Run ID, backend SHA, and summary path are required");
}
if (!/^https?:\/\/(?:[a-zA-Z0-9.-]+|\[[0-9a-fA-F:]+\])(?::[0-9]{1,5})?$/.test(baseUrl ?? "")) {
    throw new Error("A plain test origin is required");
}

const fixtures = JSON.parse(open(__ENV.PERF_FIXTURES_PATH || "/fixtures/fixtures.json"));
const fixture = testCase.fixture ? fixtures.cases?.[testCase.fixture] : {};
if (!fixture) {
    throw new Error("Selected fixture case is missing");
}
const variants = fixture.variants ?? [{}];
if (!Array.isArray(variants) || variants.length === 0) {
    throw new Error("Fixture variants must be a non-empty array");
}

const expectedFor = (variant) => ({ status: 200, ...testCase.expected, ...fixture.expected, ...variant.expected });
const requestTargets = variants.map((variant) => {
    const params = { ...testCase.params, ...fixture.params, ...variant.params };
    const query = { ...testCase.query, ...fixture.query, ...variant.query };
    const path = operation.path.replace(/\{([a-zA-Z]+)\}/g, (_, name) => {
        const value = params[name];
        if (typeof value !== "string" && typeof value !== "number") {
            throw new Error("Required route parameter is missing");
        }
        return encodeURIComponent(value);
    });
    const search = Object.entries(query).flatMap(([key, values]) =>
        (Array.isArray(values) ? values : [values])
            .map((value) => encodeURIComponent(key) + "=" + encodeURIComponent(value))
    ).join("&");
    const body = operation.method === "POST"
        ? JSON.stringify({ ...testCase.body, ...fixture.body, ...variant.body })
        : null;
    return {
        actor: variant.actor ?? fixture.actor,
        body,
        url: baseUrl + path + (search ? "?" + search : ""),
        expected: expectedFor(variant),
    };
});
const actorNames = [...new Set(requestTargets.map((target) => target.actor).filter(Boolean))];
const accountPassword = actorNames.length
    ? (__ENV.PERF_PASSWORD_PATH ? open(__ENV.PERF_PASSWORD_PATH).replace(/\r?\n$/, "")
        : __ENV.MATCHURI_PERF_ACCOUNT_PASSWORD)
    : null;
if (actorNames.length && !accountPassword) {
    throw new Error("The fixture login password is required");
}

function wholeSeconds(name, defaultValue, min, max) {
    const value = __ENV[name] ?? String(defaultValue);
    const match = /^(\d+)s?$/.exec(value);
    const parsed = match ? Number(match[1]) : NaN;
    if (!Number.isInteger(parsed) || parsed < min || parsed > max) {
        throw new Error("Invalid duration option " + name);
    }
    return parsed;
}

function integer(name, defaultValue, min, max) {
    const value = __ENV[name] ?? String(defaultValue);
    const parsed = /^\d+$/.test(value) ? Number(value) : NaN;
    if (!Number.isInteger(parsed) || parsed < min || parsed > max) {
        throw new Error("Invalid numeric option " + name);
    }
    return parsed;
}

const isSse = operation.kind === "sse";
const oneShot = testCase.oneShot === true;
const expectedRateUnit = isSse || oneShot ? "1m" : "1s";
const rateUnit = __ENV.PERF_RATE_UNIT || expectedRateUnit;
if (rateUnit !== expectedRateUnit) {
    throw new Error("Invalid rate unit for the selected case");
}
const rate = integer("PERF_RATE", isSse ? 4 : 1, 1, 1000);
const duration = wholeSeconds("PERF_DURATION", isSse ? 20 : 15, 1, 1800);
const warmup = wholeSeconds("PERF_WARMUP", 0, 0, 600);
const preAllocatedVUs = integer("PERF_VUS", isSse ? 10 : 4, 1, 1000);
const maxVUs = integer("PERF_MAX_VUS", preAllocatedVUs, preAllocatedVUs, 1000);
if (oneShot && (rate !== 1 || duration !== 15 || warmup !== 0 || maxVUs !== 1)) {
    throw new Error("An expiration-transition case requires one scheduled request");
}
export const httpTimeout = wholeSeconds("PERF_HTTP_TIMEOUT", 5, 1, 30);
export const sseHold = wholeSeconds("PERF_SSE_HOLD_SECONDS", 70, 35, 300);
export const sseTimeout = sseHold + httpTimeout;
const gracefulStop = (isSse ? sseTimeout : httpTimeout) + 1;
const measurementOffset = warmup ? warmup + gracefulStop : 0;

export const config = { operation, suite, isSse, baseUrl };
export const metrics = {
    started: new Counter("perf_started"),
    completed: new Counter("perf_completed"),
    failed: new Counter("perf_failed"),
    serverError: new Counter("perf_server_error"),
    unexpectedStatus: new Counter("perf_unexpected_status"),
    contractFailure: new Counter("perf_contract_failure"),
    completedInWindow: new Counter("perf_completed_in_window"),
    successInWindow: new Counter("perf_success_in_window"),
    measurementStart: new Gauge("perf_measurement_start_ms"),
    targetRate: new Gauge("perf_target_rate"),
    httpDuration: new Trend("perf_http_duration", true),
    sseFirstEvent: new Trend("perf_sse_first_event", true),
    sseConnected: new Counter("perf_sse_connected"),
    sseClosed: new Counter("perf_sse_closed"),
    sseHeartbeat: new Counter("perf_sse_heartbeat"),
    sseUnexpectedDisconnect: new Counter("perf_sse_unexpected_disconnect"),
};

function arrivalScenario(seconds, start, phase) {
    return {
        executor: "constant-arrival-rate",
        rate,
        timeUnit: rateUnit,
        duration: seconds + "s",
        startTime: start + "s",
        preAllocatedVUs,
        maxVUs,
        gracefulStop: gracefulStop + "s",
        tags: { phase },
    };
}

const measurementTag = "{phase:measurement}";
const thresholds = {};
for (const metric of [
    "perf_started", "perf_completed", "perf_failed", "perf_server_error",
    "perf_completed_in_window", "perf_success_in_window", "perf_unexpected_status", "perf_contract_failure",
    ...(isSse ? ["perf_sse_connected", "perf_sse_closed", "perf_sse_heartbeat",
        "perf_sse_unexpected_disconnect"] : []),
]) {
    thresholds[metric + measurementTag] = ["count>=0"];
}
thresholds[(isSse ? "perf_sse_first_event" : "perf_http_duration") + measurementTag] = ["p(50)>=0"];
const measurementDrops = "dropped_iterations{scenario:measurement}";
thresholds[measurementDrops] = ["count>=0"];
if (suite === "smoke") {
    thresholds["perf_started" + measurementTag] = [oneShot ? "count==1" : "count>0"];
    thresholds["perf_failed" + measurementTag] = ["count==0"];
    thresholds[measurementDrops] = ["count==0"];
}

export const options = {
    scenarios: {
        ...(warmup ? { warmup: arrivalScenario(warmup, 0, "warmup") } : {}),
        measurement: arrivalScenario(duration, measurementOffset, "measurement"),
    },
    setupTimeout: "60s",
    maxRedirects: 0,
    discardResponseBodies: true,
    summaryTrendStats: ["count", "p(50)", "p(95)", "p(99)"],
    systemTags: ["status", "method", "name", "scenario", "expected_response"],
    tags: { run_id: runId, operation_id: operation.id, case_id: testCase.id, kind: operation.kind },
    thresholds,
};

export function parseJson(response) {
    try {
        return response.json();
    } catch (_) {
        return undefined;
    }
}

export function setup() {
    const identityResponse = http.get(baseUrl + "/__perf/identity", {
        redirects: 0,
        responseType: "text",
        timeout: httpTimeout + "s",
        tags: { phase: "setup", name: "perf.identity" },
    });
    const identity = parseJson(identityResponse);
    if (identityResponse.status !== 200 || identity?.environment !== "matchuri-perf" ||
        !fixtures.dataset || identity.dataset !== fixtures.dataset ||
        !fixtures.fingerprint || identity.fingerprint !== fixtures.fingerprint ||
        !fixtures.anchor || identity.anchor !== fixtures.anchor ||
        identity.backend_sha !== backendSha) {
        throw new Error("The test origin did not verify its identity");
    }
    if (oneShot && !identity.first_expiration_available?.includes(testCase.fixture)) {
        throw new Error("The first-expiration fixture is unavailable");
    }

    const tokens = {};
    const requiredLifetime = measurementOffset + duration + gracefulStop + 90;
    for (const actorName of actorNames) {
        const loginId = fixtures.users?.[actorName]?.loginId;
        if (typeof loginId !== "string" || !loginId) {
            throw new Error("The fixture login is missing");
        }
        const response = http.post(baseUrl + "/api/v1/auth/login", JSON.stringify({
            loginId,
            password: accountPassword,
            captchaToken: "perf",
        }), {
            headers: { "Content-Type": "application/json" },
            redirects: 0,
            responseType: "text",
            timeout: httpTimeout + "s",
            tags: { phase: "setup", name: "perf.login" },
        });
        const payload = parseJson(response);
        if (response.status !== 200 || payload?.success !== true ||
            typeof payload.data?.accessToken !== "string" ||
            !(payload.data?.expiresIn > requiredLifetime)) {
            throw new Error("A fixture login failed or the token lifetime is too short");
        }
        tokens[actorName] = payload.data.accessToken;
    }
    return tokens;
}

export function beginIteration(tokens) {
    const phase = exec.scenario.name;
    const phaseDuration = phase === "measurement" ? duration : warmup;
    if (phase === "measurement" && exec.scenario.iterationInInstance === 0) {
        metrics.measurementStart.add(exec.scenario.startTime);
    }
    // The executor may schedule a final iteration as its time limit fires.
    if (Date.now() >= exec.scenario.startTime + phaseDuration * 1000) {
        return null;
    }
    const target = requestTargets[exec.scenario.iterationInInstance % requestTargets.length];
    const token = tokens?.[target.actor];
    metrics.started.add(1);
    metrics.targetRate.add(rate / (rateUnit === "1m" ? 60 : 1));
    return { ...target, token };
}

export function finishIteration(failed, serverError) {
    const phaseDuration = exec.scenario.name === "measurement" ? duration : warmup;
    const inWindow = Date.now() < exec.scenario.startTime + phaseDuration * 1000;
    metrics.completed.add(1);
    metrics.failed.add(Number(failed));
    metrics.serverError.add(Number(serverError));
    metrics.completedInWindow.add(Number(inWindow));
    metrics.successInWindow.add(Number(inWindow && !failed));
}

function metricValue(data, metric, stat, phase = true) {
    const name = metric + (phase ? measurementTag : "");
    return data.metrics[name]?.values?.[stat];
}

function count(data, metric) {
    return metricValue(data, metric, "count") ?? 0;
}

function percentiles(data, metric) {
    const countValue = count(data, metric);
    return {
        count: countValue,
        p50: countValue ? metricValue(data, metric, "p(50)") : null,
        p95: countValue ? metricValue(data, metric, "p(95)") : null,
        p99: countValue ? metricValue(data, metric, "p(99)") : null,
    };
}

export function handleSummary(data) {
    const start = metricValue(data, "perf_measurement_start_ms", "value", false) ?? null;
    const started = count(data, "perf_started");
    const completed = count(data, "perf_completed");
    const completedInWindow = count(data, "perf_completed_in_window");
    const failed = count(data, "perf_failed");
    const summary = {
        schema: 1,
        run_id: runId,
        operation_id: operation.id,
        case_id: testCase.id,
        suite,
        kind: operation.kind,
        target_rate: rate,
        rate_unit: rateUnit,
        duration_seconds: duration,
        warmup_seconds: warmup,
        graceful_stop_seconds: gracefulStop,
        measurement: {
            start_ms: start,
            end_ms: start === null ? null : start + duration * 1000,
            started,
            completed,
            completed_in_window: completedInWindow,
            completed_after_window: completed - completedInWindow,
            failed,
            server_errors: count(data, "perf_server_error"),
            unexpected_statuses: count(data, "perf_unexpected_status"),
            contract_failures: count(data, "perf_contract_failure"),
            dropped: metricValue(data, measurementDrops, "count", false) ?? 0,
            started_per_second: started / duration,
            completed_per_second: completedInWindow / duration,
            success_per_second: count(data, "perf_success_in_window") / duration,
            error_rate: completed ? failed / completed : null,
            latency_ms: percentiles(data, "perf_http_duration"),
        },
    };
    if (isSse) {
        summary.sse = {
            hold_seconds: sseHold,
            connected: count(data, "perf_sse_connected"),
            closed: count(data, "perf_sse_closed"),
            heartbeat: count(data, "perf_sse_heartbeat"),
            unexpected_disconnect: count(data, "perf_sse_unexpected_disconnect"),
            first_event_ms: percentiles(data, "perf_sse_first_event"),
        };
    }
    return { [summaryPath]: JSON.stringify(summary, null, 2) + "\n" };
}
