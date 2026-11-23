export class TraceApiError extends Error {
  constructor(message, status, detail) {
    super(message);
    this.name = "TraceApiError";
    this.status = status;
    this.detail = detail;
  }
}

export function createTraceClient(token) {
  async function request(path) {
    const response = await fetch(path, {
      headers: { Accept: "application/json", Authorization: `Bearer ${token}` }
    });
    const contentType = response.headers.get("content-type") || "";
    const payload = contentType.includes("application/json") ? await response.json() : await response.text();
    if (!response.ok) {
      const detail = typeof payload === "object" && payload !== null ? payload.detail : payload;
      throw new TraceApiError(`Trace API failed: ${response.status}`, response.status, detail);
    }
    return payload;
  }

  return {
    runs: (params) => request(`/trace-api/runs?${params.toString()}`),
    run: (requestId) => request(`/trace-api/runs/${encodeURIComponent(requestId)}`),
    modelIo: (requestId, spanId) =>
      request(`/trace-api/runs/${encodeURIComponent(requestId)}/model-io/${encodeURIComponent(spanId)}`)
  };
}
