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
    created_at: "2026-08-25T00:00:00Z",
  },
];
function render(selectedService: string, available = true) {
  return renderToStaticMarkup(
    <ServiceManagement
      client={createAdministrationClient()}
      csrf="synthetic-csrf"
      onNotice={() => undefined}
      onRefresh={() => Promise.resolve()}
      onSelect={() => undefined}
      onOpenDetails={() => undefined}
      selectedService={selectedService}
      services={available ? services : []}
      available={available}
    />,
  );
}
describe("contextual service creation", () => {
  for (const service of services)
    it(`identifies ${service.api_name} as the only creation parent`, () => {
      const markup = render(service.api_name);
      expect(markup.match(/aria-label="New service under [^"]+"/g)).toEqual([
        `aria-label="New service under ${service.display_name}, API name ${service.api_name}"`,
      ]);
      expect(markup).toMatch(
        /class="od-graph-canvas[^]*aria-label="New service under/,
      );
      expect(markup.match(/tabindex="0"/g)).toHaveLength(1);
      expect(markup).not.toMatch(/Create a root service|No parent/);
    });
  it("has no creation action before a confirmed graph is available", () => {
    expect(render("", false)).not.toMatch(
      /New service|Create service|No parent/,
    );
  });
});
