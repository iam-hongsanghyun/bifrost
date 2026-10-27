/**
 * Turning tool traffic into something a modeller can read.
 *
 * The agent's raw stream is honest but unreadable: `mcp__ragnarok__add_generator`
 * with 400 characters of JSON attached, and SDK plumbing like `ToolSearch` dumping
 * a list of tool references. None of that tells the user what is happening to their
 * model, and it buries the parts that do.
 *
 * So this module answers three questions per event — what is happening, in words;
 * is this plumbing the user should not have to read; and is the result worth
 * showing. It is deliberately rule-based on the verb rather than a table of all 154
 * Ragnarok tools: a table would silently fall back to raw names the day a tool is
 * added, which is the failure mode being fixed. Whole-tool exceptions below are for
 * the names whose verb alone would mislead (`submit_*` queues a solve, `check_*`
 * reads the model back).
 */

/** Prefixes the MCP servers put on their tools. */
const MCP_PREFIXES = ['mcp__ragnarok__', 'mcp__bifrost__'];

/**
 * Verb → phrasing. Order matters: longer, more specific keys first, since these
 * are matched as prefixes.
 */
const VERBS: ReadonlyArray<readonly [string, (rest: string) => string]> = [
  ['get_world_state', () => 'Reading the model'],
  ['describe_component', (r) => `Checking what ${subject(r) || 'a component'} accepts`],
  ['validate_model', () => 'Validating the model'],
  ['check_model', () => 'Checking the model against what you asked for'],
  ['consistency_check', () => 'Running PyPSA\'s consistency check'],
  ['submit_scenarios', () => 'Queueing scenarios'],
  ['submit_scenario', () => 'Queueing a scenario'],
  ['submit_solve', () => 'Queueing a solve'],
  ['cancel_solve', () => 'Cancelling a solve'],
  ['temporal_cluster', () => 'Clustering the time axis'],
  ['one_click_model', () => 'Building a model from a country'],
  ['build_starter_pack', () => 'Building a starter pack'],
  ['network_analysis', () => 'Analysing the grid topology'],
  ['forge_query', () => 'Bulk-editing the model'],
  ['merge_fragment', () => 'Merging a data fragment into the model'],
  ['merge_models', () => 'Merging models'],
  ['slice_model', () => 'Slicing the model'],
  ['snap_to_buses', () => 'Snapping assets to buses'],
  ['siting_scan', () => 'Scanning candidate sites'],
  ['undo_journal_entry', () => 'Undoing a change'],
  ['revert_session', () => 'Rolling the model back'],
  ['run_plugin_', (r) => `Running the ${subject(r)} plugin`],
  ['compare_', (r) => `Comparing ${subject(r)}`],
  ['check_', (r) => `Checking ${subject(r)}`],
  ['save_', (r) => `Saving ${article(subject(r))}`],
  ['submit_', (r) => `Queueing ${subject(r)}`],
  ['load_', (r) => `Loading ${subject(r)}`],
  ['install_', (r) => `Installing ${subject(r)}`],
  ['uninstall_', (r) => `Uninstalling ${subject(r)}`],
  ['attach_', (r) => `Attaching ${subject(r)}`],
  ['adjust_', (r) => `Adjusting ${subject(r)}`],
  ['apply_', (r) => `Applying ${subject(r)}`],
  ['revert_', (r) => `Reverting ${subject(r)}`],
  ['rename_', (r) => `Renaming ${subject(r)}`],
  ['promote_', (r) => `Promoting ${subject(r)}`],
  ['rerun_', (r) => `Re-running ${subject(r)}`],
  ['retarget_', (r) => `Retargeting ${subject(r)}`],
  ['forecast_', (r) => `Forecasting ${subject(r)}`],
  ['driver_forecast', () => 'Forecasting demand from drivers'],
  ['transform_', (r) => `Transforming ${subject(r)}`],
  ['derive_', (r) => `Deriving ${subject(r)}`],
  ['refresh_', (r) => `Refreshing ${subject(r)}`],
  ['reload_', (r) => `Reloading ${subject(r)}`],
  ['upload_', (r) => `Uploading ${subject(r)}`],
  ['clear_', (r) => `Clearing ${subject(r)}`],
  ['pypsa_earth_', (r) => `PyPSA-Earth: ${subject(r)}`],
  ['physical_risk_', (r) => `Physical risk: ${subject(r)}`],
  ['get_', (r) => `Reading ${subject(r)}`],
  ['list_', (r) => `Listing ${subject(r)}`],
  ['read_', (r) => `Reading ${subject(r)}`],
  ['describe_', (r) => `Checking ${subject(r)}`],
  ['add_', (r) => `Adding ${article(subject(r))}`],
  ['create_', (r) => `Creating ${article(subject(r))}`],
  ['set_', (r) => `Setting ${subject(r)}`],
  ['update_', (r) => `Updating ${subject(r)}`],
  ['patch_', (r) => `Editing ${subject(r)}`],
  ['delete_', (r) => `Removing ${subject(r)}`],
  ['remove_', (r) => `Removing ${subject(r)}`],
  ['export_', (r) => `Exporting ${subject(r)}`],
  ['import_', (r) => `Importing ${subject(r)}`],
  ['solve', () => 'Solving'],
  ['run_', (r) => `Running ${subject(r)}`],
  ['optimize_', (r) => `Optimising ${subject(r)}`],
  ['cluster_', (r) => `Reducing ${subject(r)}`],
  ['ask_user', () => 'Asking you'],
];

function subject(raw: string): string {
  return raw.replace(/_/g, ' ').trim();
}

function article(noun: string): string {
  if (!noun) return 'something';
  return /^[aeiou]/i.test(noun) ? `an ${noun}` : `a ${noun}`;
}

/** Strip the MCP server prefix, leaving the tool's own name. */
export function bareToolName(tool: string): string {
  for (const prefix of MCP_PREFIXES) {
    if (tool.startsWith(prefix)) return tool.slice(prefix.length);
  }
  return tool;
}

/**
 * Whether this event is plumbing rather than work on the model.
 *
 * Anything that is not one of our MCP tools is the SDK's own machinery —
 * `ToolSearch` discovering tools being the noisy one. The user asked for a model,
 * not a transcript of how the agent found its own toolbox.
 */
export function isPlumbing(tool: string | undefined): boolean {
  if (!tool) return true;
  return !MCP_PREFIXES.some((p) => tool.startsWith(p)) && !VERBS.some(([v]) => tool.startsWith(v));
}

/** What is happening, in words. */
export function describeTool(tool: string | undefined): string {
  if (!tool) return 'Working';
  const bare = bareToolName(tool);
  for (const [verb, phrase] of VERBS) {
    if (bare === verb) return phrase('');
    if (bare.startsWith(verb)) return phrase(bare.slice(verb.length));
  }
  // Unknown tool: say its name in words rather than showing an identifier. Better
  // a slightly stiff sentence than `mcp__ragnarok__frobnicate_bus`.
  const words = subject(bare);
  return words.charAt(0).toUpperCase() + words.slice(1);
}

/** Past tense, for a call that has finished. */
export function describeToolDone(tool: string | undefined, ok: boolean): string {
  const present = describeTool(tool);
  if (!ok) return `${present} — failed`;
  return present
    .replace(/^Reading /, 'Read ')
    .replace(/^Listing /, 'Listed ')
    .replace(/^Checking /, 'Checked ')
    .replace(/^Adding /, 'Added ')
    .replace(/^Creating /, 'Created ')
    .replace(/^Setting /, 'Set ')
    .replace(/^Updating /, 'Updated ')
    .replace(/^Editing /, 'Edited ')
    .replace(/^Removing /, 'Removed ')
    .replace(/^Exporting /, 'Exported ')
    .replace(/^Importing /, 'Imported ')
    .replace(/^Running /, 'Ran ')
    .replace(/^Optimising /, 'Optimised ')
    .replace(/^Reducing /, 'Reduced ')
    .replace(/^Validating /, 'Validated ')
    .replace(/^Comparing /, 'Compared ')
    .replace(/^Saving /, 'Saved ')
    .replace(/^Queueing /, 'Queued ')
    .replace(/^Loading /, 'Loaded ')
    .replace(/^Installing /, 'Installed ')
    .replace(/^Uninstalling /, 'Uninstalled ')
    .replace(/^Attaching /, 'Attached ')
    .replace(/^Adjusting /, 'Adjusted ')
    .replace(/^Applying /, 'Applied ')
    .replace(/^Reverting /, 'Reverted ')
    .replace(/^Renaming /, 'Renamed ')
    .replace(/^Promoting /, 'Promoted ')
    .replace(/^Re-running /, 'Re-ran ')
    .replace(/^Retargeting /, 'Retargeted ')
    .replace(/^Forecasting /, 'Forecast ')
    .replace(/^Transforming /, 'Transformed ')
    .replace(/^Deriving /, 'Derived ')
    .replace(/^Refreshing /, 'Refreshed ')
    .replace(/^Reloading /, 'Reloaded ')
    .replace(/^Uploading /, 'Uploaded ')
    .replace(/^Clearing /, 'Cleared ')
    .replace(/^Clustering /, 'Clustered ')
    .replace(/^Cancelling /, 'Cancelled ')
    .replace(/^Undoing /, 'Undid ')
    .replace(/^Merging /, 'Merged ')
    .replace(/^Slicing /, 'Sliced ')
    .replace(/^Snapping /, 'Snapped ')
    .replace(/^Scanning /, 'Scanned ')
    .replace(/^Rolling /, 'Rolled ')
    .replace(/^Bulk-editing /, 'Bulk-edited ')
    .replace(/^Analysing /, 'Analysed ')
    .replace(/^Building /, 'Built ')
    .replace(/^Solving$/, 'Solved')
    .replace(/^Asking you$/, 'Asked you');
}

/**
 * A result worth showing, or null to show nothing.
 *
 * Raw tool output is JSON far more often than it is a sentence, and a truncated
 * object is noise with a shape. Only a short, prose-looking result is surfaced;
 * everything else is dropped, because the outcome is already carried by the
 * done/failed state and the count of changes.
 */
export function humanSummary(summary: string | undefined): string | null {
  const text = (summary ?? '').trim();
  if (!text) return null;
  // Structured payloads and tool-reference dumps: never useful inline.
  if (/^[[{]/.test(text) || text.includes("'type':") || text.includes('"type":')) return null;
  if (text.includes('tool_reference') || text.includes('tool_name')) return null;
  // A wall of text is not a summary either.
  if (text.length > 120 || text.includes('\n')) return null;
  return text;
}

/**
 * Whether an agent state means "still working".
 *
 * Mirrors the server's own `Session.busy` exactly, because two definitions of busy
 * is how the composer got stuck: the service publishes `done` and only then sets
 * `state = "idle"` internally, without publishing it — so the last state a client
 * ever sees is `done`, and a client testing `state !== 'idle'` waits forever.
 *
 * `awaiting_input` and `awaiting_approval` are handled the way the server does:
 * awaiting_input is NOT busy, because the whole point is that the user must be able
 * to answer.
 */
export function isWorkingState(state: string | undefined): boolean {
  return state === 'thinking' || state === 'running_tool' || state === 'awaiting_approval';
}
