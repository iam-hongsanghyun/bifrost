/**
 * First run: which Ragnarok, and start it.
 *
 * Bifrost is a wrapper, so it cannot assume an engine is already running. This is
 * the screen that turns a fresh install into a working one — pick a checkout, and
 * Bifrost remembers it and launches it.
 *
 * The browser cannot read a real directory path from a file picker (it hands back
 * a sandboxed handle, not a filesystem location), so the path is typed or pasted,
 * with detected checkouts offered as one-click options. Validation happens
 * server-side, where the filesystem actually is, and says WHY a directory was
 * refused rather than just rejecting it.
 */
import { useState } from 'react';
import { EngineInfo, EngineRootCheck, engineApi } from '../api';

interface Props {
  info: EngineInfo;
  onChanged: () => void;
}

export function EngineSetup({ info, onChanged }: Props) {
  const [path, setPath] = useState(info.root ?? '');
  const [check, setCheck] = useState<EngineRootCheck | null>(info.check ?? null);
  const [busy, setBusy] = useState(false);
  const [log, setLog] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);

  const validate = async (candidate: string) => {
    setError(null);
    if (!candidate.trim()) { setCheck(null); return; }
    setCheck(await engineApi.check(candidate));
  };

  const use = async (candidate: string) => {
    setBusy(true);
    setError(null);
    try {
      await engineApi.setRoot(candidate);
      onChanged();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not use that directory.');
    } finally {
      setBusy(false);
    }
  };

  const start = async () => {
    setBusy(true);
    setError(null);
    try {
      const result = await engineApi.start();
      if (!result.started) setError(result.reason);
      // A start that failed at import shows up in the engine's own log, not here.
      setLog((await engineApi.log()).lines);
      onChanged();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not start Ragnarok.');
      setLog((await engineApi.log().catch(() => ({ lines: [] }))).lines);
    } finally {
      setBusy(false);
    }
  };

  const running = info.process.running;
  const ownedByUs = info.process.owner === 'bifrost';
  // A DIFFERENT Ragnarok checkout is holding the port. Silence here is what let
  // Bifrost drive another clone: same API, other code, other data directory, and
  // the model the agent built nowhere to be seen.
  const foreign = info.process.owner === 'foreign' ? info.process.foreignRoot : null;

  return (
    <div className="setup">
      <div className="setup-card">
        <h1>Connect Bifrost to Ragnarok</h1>
        <p className="muted">
          Bifrost is the agent; Ragnarok is the modelling engine. Point Bifrost at your
          Ragnarok folder once and it will start it for you from now on.
        </p>

        {foreign && (
          <div className="banner banner--error" role="alert">
            Port {info.process.port} is served by <code>{foreign}</code>, not by the
            Ragnarok you selected. Bifrost will not use it — it is a different checkout,
            with its own data directory and its own model. Stop it, or give Bifrost a free
            port with <code>RAGNAROK_API_BASE</code>.
          </div>
        )}

        {info.suggestions.length > 0 && (
          <section className="setup-block">
            <h2>Found on this machine</h2>
            {info.suggestions.map((s) => (
              <button key={s.path} className="setup-suggestion" disabled={busy}
                onClick={() => void use(s.path)}>
                <code>{s.path}</code>
                <span className="muted">{s.interpreter ? 'ready' : 'needs its environment built'}</span>
              </button>
            ))}
          </section>
        )}

        <section className="setup-block">
          <h2>Or give the folder</h2>
          <p className="muted setup-hint">
            The folder containing <code>backend/</code> and <code>frontend/</code> — in Finder,
            right-click it and hold <kbd>Option</kbd> to “Copy as Pathname”.
          </p>
          <div className="setup-row">
            <input
              value={path}
              placeholder="/Users/you/github/ragnarok"
              onChange={(e) => { setPath(e.target.value); void validate(e.target.value); }}
              spellCheck={false}
            />
            <button className="btn-primary" disabled={busy || !check?.valid} onClick={() => void use(path)}>
              Use this
            </button>
          </div>
          {check && (
            <p className={check.valid ? 'setup-ok' : 'setup-bad'}>
              {check.valid ? '✓ ' : '✕ '}{check.reason}
            </p>
          )}
        </section>

        {info.configured && (
          <section className="setup-block">
            <h2>Engine</h2>
            <p className="muted">
              Using <code>{info.root}</code> on <code>{info.apiBase}</code>.
            </p>
            <div className="setup-row">
              {running ? (
                <>
                  <span className="chip">{ownedByUs ? 'running (started by Bifrost)' : 'running (started outside Bifrost)'}</span>
                  {ownedByUs && (
                    <button className="btn-ghost" disabled={busy}
                      onClick={() => void engineApi.stop().then(onChanged)}>
                      Stop engine
                    </button>
                  )}
                  <button className="btn-primary" onClick={onChanged}>Continue</button>
                </>
              ) : (
                <button className="btn-primary" disabled={busy} onClick={() => void start()}>
                  {busy ? 'Starting…' : 'Start Ragnarok'}
                </button>
              )}
            </div>
            {!running && info.check && !info.check.interpreter && (
              <p className="setup-bad">
                This Ragnarok has no environment yet. Run its own <code>run.command</code> once to
                build <code>.venv-pypsa</code>, then start it from here.
              </p>
            )}
          </section>
        )}

        {error && <p className="setup-bad" role="alert">{error}</p>}
        {log.length > 0 && (
          <details className="setup-block" open>
            <summary>Engine output</summary>
            <pre className="setup-log">{log.join('\n')}</pre>
          </details>
        )}

        <p className="muted setup-foot">Bifrost is running on {info.bifrostPython}</p>
      </div>
    </div>
  );
}
