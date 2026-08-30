import type { RequestLogSummary } from "./api.ts";

export function requestLogActorLabel(item: RequestLogSummary): string {
  return item.call_actor === "service" ? "Service" : "Administrator";
}

export function requestLogScopeLabel(item: RequestLogSummary): string {
  if (item.call_actor === "service")
    return `${item.service_api_name} / ${item.workspace_api_name}`;
  if (item.assignment_api_name !== undefined)
    return `${item.administrator_subject} / configuration ${item.configuration_service_api_name}`;
  return `${item.administrator_subject} / global exact route`;
}

export function requestLogRouteLabel(item: RequestLogSummary): string {
  return (
    item.assignment_api_name ?? item.provider_model_api_name ?? "Unavailable"
  );
}
