# English names for every interface

Router 0.9.0 and agent 0.20.0 rename everything that leaves the process to English: Home Assistant entities,
event types and attributes, journal events and reasons, GPU guard states and fields, file and module names.
The rest of the code (identifiers, log and error texts, comments) follows in later changes; this document
is the migration plan for the interface part, which existing installations have to follow.

## Scope and order

1. **Interfaces (this change).** Everything another program, an automation or a dashboard can see.
2. **Code identifiers, log and error texts.** No migration needed: nothing outside the process depends on them.
3. **Comments and docstrings**, by module group. German translations of design documents (`*.de.md`)
   and the German summary in release notes stay German on purpose.

## Names

### Home Assistant entities

Entity IDs are now set explicitly with `default_entity_id` (MQTT discovery). Before, Home Assistant derived
them from the German display names, for example `sensor.skirnir_knoten_online`. Display names stay
German, the entity IDs become English:

| Router entity (unique_id `skirnir_router_<oid>`) | New entity ID |
|---|---|
| `nodes_online`, `nodes_busy`, `models_available`, `roles_ready`, `nodes_pending` | `sensor.skirnir_<oid>` |
| `problem`, `pending` | `binary_sensor.skirnir_<oid>` |
| `requests_5min`, `requests_today`, `cloud_spend`, `tokens_today`, `last_route`, `ollama_updates` | `sensor.skirnir_<oid>` |
| `kuerzungen` | `sensor.skirnir_truncations` |
| `kuerzung` | `event.skirnir_truncation` |
| `node_<node>_state`, `_gpu_util`, `_vram_free`, `_game`, `_ram_free`, `_commit_free` | `sensor.skirnir_node_<node>_<field>` |

Agent entities (unique_id `<device>_<oid>`) get `<domain>.<device>_<oid>`. The three truncation entities
are renamed: `ollama_kuerzungen` → `ollama_truncations`, `ollama_letzte_kuerzung` →
`ollama_last_truncation`, `ollama_kuerzung` → `ollama_truncation`.

**The unique IDs and discovery topics of renamed entities stay as they are.** A new unique ID would
create a second entity and leave the old one, with its history and every automation, as an orphan.
Only the entity ID changes.

### Events and attributes

| Before | After |
|---|---|
| Router event `kontext_gekuerzt` (journal and HA) | `context_truncated` |
| its fields `geschaetzt`, `angekommen`, `quote`, `sicher` | `estimated`, `received`, `ratio`, `certain` |
| HA state fields `kuerzungen`, `letzte_kuerzung`, attribute `letzte` | `truncations`, `last_truncation`, `last` |
| MQTT topic `<base>/kuerzung` | `<base>/truncation` |
| Agent truncation types `eingabe_gekuerzt`, `kontext_voll` | `input_truncated`, `context_full` |
| their fields `art`, `zeit`, `neu` | `type`, `at`, `truncated_to` |
| GPU guard states `aus`, `unverfuegbar`, `hochlast`, `gedrosselt`, `erholung` | `off`, `unavailable`, `high_load`, `throttled`, `recovery` |
| GPU guard events `limit_gesetzt`, `limit_verweigert`, `fremdeingriff`, `spannung_niedrig`, `temperatur_hoch`, `hw_drossel`, `abgewaehlt` | `limit_set`, `limit_refused`, `external_change`, `voltage_low`, `temperature_high`, `hw_throttle`, `deselected` |
| GPU guard fields `quelle`, `grund`, `warnungen`, `hochlast_s`, `zeit` | `source`, `reason`, `warnings`, `high_load_s`, `at` |
| GPU guard attributes `letztes_ereignis`, `letztes_ereignis_zeit`, `ereignisse` | `last_event`, `last_event_at`, `events` |
| Journal reasons `spiel-leicht`, `spiel-vorbei`, `ausweichen` | `game_light`, `game_ended`, `fallback` |
| Shadow statuses `cloud uebersprungen`, `kein Knoten` | `cloud_skipped`, `no_node` |
| Measurement/benchmark steps (`start`, `aufwärmen`, `Messlauf`, `fertig`, `fehler`, ...) | `start`, `warmup`, `run`, `done`, `error`, ... |
| Catalog `source` values `gemessen`, `gemessen (nvidia-smi)`, `geschaetzt` (roles.yaml) | `measured`, `measured (nvidia-smi)`, `estimated` |
| Node game sensor without a game: `keins` | `none` |
| Metric `skirnir_node_gpu_guard` label `state` | the English GPU guard states above |

Catalog entries already in `roles.yaml` keep their old `source` value on disk; the router reports them in
English. This also fixes the UI, which showed models measured with nvidia-smi as estimated.

### Files, modules and commands

| Before | After |
|---|---|
| `anfragen.jsonl` (request log of the observation mode) | `requests.jsonl`; the router renames an existing file on start |
| `kontextpruefung.py`, `ausweich_beobachtung.py` | `context_check.py`, `fallback_observation.py` |
| agent verb and module `spiel` | `game` |

### Code identifiers and messages (router 0.9.1 / agent 0.20.1)

Nothing outside the code sees these, so they follow in a release of their own:

- Go files `spiel*.go` → `game*.go`, `kuerzung*.go` → `truncation*.go`; German Go and Python identifiers → English.
- Log lines, API and validation errors, `router.py --check` and the agent's command-line output are English.
  `deploy.py` checks for `configuration ok` instead of `Konfiguration ok`.

These texts stay German on purpose, because they end up in front of the operator in German:

- Home Assistant problem texts and display names. This includes the GPU guard reasons and events and the
  agent's and Ollama's update errors, which the router passes into the Home Assistant problem list.
- The labels and help texts of the settings UI (bilingual).
- The German keywords of the `rules` decision engine and the German prompts (`local_llm`, measurement prompt).

### Comments and docstrings

They follow by module group, each as a pull request that changes no code token (checked against the base before merging):

1. Router (`router/`): done.
2. Agent (`agent/`): done.
3. Tests, test file names and `deploy/` output.

Roadmap steps from `design/roadmap.md` are "stages" (`Stufe 3` → `Stage 3`); "tier" stays reserved for the tiers of a role.

## Compatibility

- **Router 0.9.0 reads both names from agents.** It translates GPU guard states and fields of agents before
  0.20.0, so mixed fleets keep working while the agent update rolls out node by node.
- **The journal is translated on load.** Entries in `events.jsonl` written before 0.9.0 are converted to
  the new event and reason names, so filters in the UI find old and new entries alike.
- **The UI shows German labels for the English values** in its German view.
- **No other compatibility layer:** Home Assistant automations have to follow the new names (below).

## Rollout

1. **Deploy router 0.9.0.** It understands old and new agents.
   - From now on the router publishes `event.skirnir_truncation` events with the type `context_truncated` and English attributes.
   - Automations that read the old router attributes see empty values until step 3.
   - Redeploy the monitoring dashboard. Its GPU guard panel colours old and new state labels, so the
     90 days of Prometheus history stay readable.
2. **Roll out agent 0.20.0, canary first.** Each updated agent publishes English truncation types and GPU guard names.
3. **Migrate Home Assistant**, right after step 1 and again whenever a further node runs 0.20.0:
   - Rename the entity IDs in the entity registry. Home Assistant moves the recorder history along with the ID.
   - Replace old entity IDs, event types and attribute names in automations, scripts and dashboards.

   The migration has to be idempotent and has to stop on any entity ID that already exists. Home Assistant
   keeps an entity's ID once registered, so a new `default_entity_id` alone changes nothing for existing installations.
