/* global window, sessionStorage */
// These helpers use only the controlled shell fixture and its synthetic values.
export async function selectService(page, value) {
  const input = page.getByRole("combobox", {
    name: "Service context",
    exact: true,
  });
  await input.click();
  await page.locator(".od-searchable-select-listbox").selectOption(value);
}

export async function reloadFixture(page) {
  await page.evaluate(() => {
    const fixture = window.shellFixture;
    sessionStorage.setItem(
      "shell-fixture-reload",
      JSON.stringify({
        values: fixture.values,
        fail: fixture.fail,
        hold: fixture.hold,
      }),
    );
  });
  await page.reload();
}
