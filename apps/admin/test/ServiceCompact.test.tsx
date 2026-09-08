import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { ServiceManagement } from "../src/ServiceManagement.tsx";
import { createAdministrationClient, type Service } from "../src/api.ts";

const services: readonly Service[] = [
  {
    api_name: "root",
    display_name: "Root display",
    parent_service_api_name: null,
    created_at: "2026-08-25T00:00:00Z",
  },
  {
    api_name: "child",
    display_name: "Child display",
    parent_service_api_name: "root",
    created_at: "2026-08-26T00:00:00Z",
  },
];
function render(selectedService: string) {
  return renderToStaticMarkup(
    <ServiceManagement
      client={createAdministrationClient()}
      csrf="test"
      onNotice={() => undefined}
      onRefresh={() => Promise.resolve()}
      onSelect={() => undefined}
      onOpenDetails={() => undefined}
      selectedService={selectedService}
      services={services}
    />,
  );
}

describe("compact service inspector", () => {
  it("shows ordered service facts and one footer action without management forms", () => {
    const markup = render("child");
    const inspector = markup.slice(
      markup.indexOf("<dialog"),
      markup.indexOf("</dialog>"),
    );
    expect(
      [...inspector.matchAll(/<dt>([^<]+)<\/dt>/g)].map((match) => match[1]),
    ).toEqual(["API name", "Parent", "Created"]);
    expect(inspector).toContain("Child display</h2>");
    expect(inspector).toContain("Child service</span>");
    expect(inspector).toContain("Root display <code>root</code>");
    expect(inspector).toContain('dateTime="2026-08-26T00:00:00.000Z"');
    const footer = inspector.slice(inspector.indexOf("<footer"));
    expect([...footer.matchAll(/<button /g)]).toHaveLength(1);
    expect(footer).toContain("Open service details</button>");
    expect(inspector).not.toMatch(
      /<input|<form|Workspaces|Service API keys|Delete service|Save changes/,
    );
  });
  it("shows no parent for the permanent root", () => {
    const markup = render("root");
    const inspector = markup.slice(
      markup.indexOf("<dialog"),
      markup.indexOf("</dialog>"),
    );
    expect(inspector).toContain("Root service</span>");
    expect(inspector).toContain("<dt>Parent</dt><dd>None</dd>");
  });
});
