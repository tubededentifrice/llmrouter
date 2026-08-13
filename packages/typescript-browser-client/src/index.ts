export * from "./generated-models.js";
export * from "./contracts.js";

/** Authority that is safe for an eligible browser session. */
export interface BrowserClientOptions {
  readonly endpoint: URL;
  readonly sessionToken: string;
}

/** Store browser client configuration until transport work starts. */
export class BrowserClient {
  public constructor(public readonly options: BrowserClientOptions) {}
}
