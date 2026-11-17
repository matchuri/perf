import http from "k6/http";
import {
    beginIteration, config, finishIteration, httpTimeout, metrics, parseJson,
} from "./common.js";
export { handleSummary, options, setup } from "./common.js";

if (config.isSse) {
    throw new Error("The selected operation requires the SSE entry point");
}

function matchesCheck(value, rule) {
    const actual = rule.path === "" ? value : rule.path.split(".").reduce((item, key) => item?.[key], value);
    if ("equals" in rule) {
        return actual === rule.equals;
    }
    if ("minimum" in rule) {
        return typeof actual === "number" && actual >= rule.minimum;
    }
    if ("length" in rule) {
        return Array.isArray(actual) && actual.length === rule.length;
    }
    if ("lengthAtLeast" in rule) {
        return Array.isArray(actual) && actual.length >= rule.lengthAtLeast;
    }
    if ("type" in rule) {
        return actual !== null && typeof actual === rule.type;
    }
    return false;
}

function matchesContract(response, expected) {
    if (expected.location) {
        return response.headers.Location === expected.location;
    }
    const payload = parseJson(response);
    if (config.operation.kind === "health") {
        return expected.checks.every((rule) => matchesCheck(payload, rule));
    }
    if (expected.errorCode) {
        return payload?.success === false && payload.data === null &&
            payload.error?.code === expected.errorCode;
    }
    return payload?.success === true && payload.error === null && "data" in payload &&
        (expected.checks ?? []).every((rule) => matchesCheck(payload.data, rule));
}

export default function (tokens) {
    const request = beginIteration(tokens);
    if (!request) {
        return;
    }
    const checkBody = config.suite === "smoke" || config.operation.kind === "health";
    const response = http.request(config.operation.method, request.url, request.body, {
        headers: {
            ...(request.token ? { Authorization: "Bearer " + request.token } : {}),
            ...(request.body ? { "Content-Type": "application/json" } : {}),
        },
        redirects: 0,
        timeout: httpTimeout + "s",
        responseType: checkBody ? "text" : "none",
        responseCallback: http.expectedStatuses(request.expected.status),
        tags: { name: config.operation.path },
    });

    const unexpectedStatus = response.status !== request.expected.status;
    const invalidContract = !unexpectedStatus && checkBody && !matchesContract(response, request.expected);
    metrics.unexpectedStatus.add(Number(unexpectedStatus));
    metrics.contractFailure.add(Number(invalidContract));
    const serverError = response.status >= 500 || response.error_code === 1050;
    if (response.status > 0 && Number.isFinite(response.timings.duration)) {
        metrics.httpDuration.add(response.timings.duration);
    }
    finishIteration(unexpectedStatus || invalidContract, serverError);
}
