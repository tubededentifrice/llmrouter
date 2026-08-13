/** Configuration that is available only to trusted server code. */
export interface ServerClientOptions {
  readonly endpoint: URL;
  readonly serviceBootstrapSecret: string;
}

/** Store server client configuration until transport work starts. */
export class ServerClient {
  public constructor(public readonly options: ServerClientOptions) {}
}
