import sse from "k6/x/sse";
import {
    beginIteration, config, finishIteration, metrics, sseHold, sseTimeout,
} from "./common.js";
export { handleSummary, options, setup } from "./common.js";

if (!config.isSse) {
    throw new Error("The selected operation requires the HTTP entry point");
}

export default function (tokens) {
    const request = beginIteration(tokens);
    if (!request) {
        return;
    }
    const startedAt = Date.now();
    let connectedAt = null;
    let heartbeatSeen = false;
    let plannedClose = false;
    let errorSeen = false;
    let closing = false;

    const response = sse.open(request.url, {
        headers: { Authorization: "Bearer " + request.token },
        tags: { name: config.operation.path },
        timeout: sseTimeout + "s",
    }, (client) => {
        client.on("event", (event) => {
            if (event.name === "REALTIME_CONNECTED" && connectedAt === null) {
                connectedAt = Date.now();
                metrics.sseConnected.add(1);
                metrics.sseFirstEvent.add(connectedAt - startedAt);
            }
            if (event.comment?.trim() === "heartbeat") {
                heartbeatSeen = true;
                metrics.sseHeartbeat.add(1);
            }
        });
        client.on("error", () => {
            if (closing) {
                return;
            }
            // xk6-sse blocks the JS event loop. Its HTTP timeout bounds the hold
            // and signals a read error once the planned interval has elapsed.
            const now = Date.now();
            plannedClose = connectedAt !== null && now - connectedAt >= sseHold * 1000 &&
                now - startedAt >= sseTimeout * 1000 - 100;
            errorSeen = !plannedClose;
            closing = true;
            client.close();
        });
    });

    const status = response?.status ?? 0;
    const unexpected = !plannedClose || errorSeen;
    const unexpectedStatus = status !== request.expected.status;
    const invalidContract = !unexpectedStatus && (connectedAt === null || !heartbeatSeen || unexpected);
    metrics.unexpectedStatus.add(Number(unexpectedStatus));
    metrics.contractFailure.add(Number(invalidContract));
    if (connectedAt !== null) {
        metrics.sseClosed.add(1);
    }
    metrics.sseUnexpectedDisconnect.add(Number(unexpected));
    const requestTimedOut = status === 0 && Date.now() - startedAt >= sseTimeout * 1000 - 100;
    finishIteration(unexpectedStatus || invalidContract, status >= 500 || requestTimedOut);
}
