export class EmbedSnapshotLoader {
  private generation = 0;
  private abort: AbortController | null = null;

  load<T>(
    operation: (signal: AbortSignal) => Promise<T>,
    onSuccess: (value: T) => void,
    onFailure: () => void,
  ): void {
    this.cancel();
    const generation = this.generation;
    const abort = new AbortController();
    this.abort = abort;
    void operation(abort.signal)
      .then((value) => {
        if (generation === this.generation && !abort.signal.aborted)
          onSuccess(value);
      })
      .catch(() => {
        if (generation === this.generation && !abort.signal.aborted)
          onFailure();
      });
  }

  cancel(): void {
    this.generation += 1;
    this.abort?.abort();
    this.abort = null;
  }
}
