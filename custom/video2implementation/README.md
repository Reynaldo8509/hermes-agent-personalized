# VIDEO2IMPLEMENTATION isolated preparation

This isolated preparation layer is not registered in Hermes and does not
change Pulse routing. Its default operations do not call YouTube, Gemini,
Composio, Telegram, or the database. The isolated Gemini client can perform
one explicitly authorized provider call through `allow_external_call=True`;
it never executes model output or creates an AgentRequest.

The contract maps existing AgentRequest fields: id -> task_id, origin ->
requester, status, and existing timestamps. Destination values are logical
aliases resolved by trusted server configuration, never by the LLM.

The versioned master prompt is stored under prompts/ and loaded only by an
explicit Pulse-side preparation call. It is not copied into MAX and is never
executed as code. gemini_preparation.py creates a provider-neutral Gemini
Interactions API envelope but sets external_call_allowed=False; no request
is sent in this phase.

metadata_sources.py plans the existing oEmbed/basic source and the official
YouTube Data API videos.list fallback. It does not call either source. The
Data API fallback is credential-gated, uses a validated video id, and keeps
chapters null unless a later source explicitly supplies them.

Hermes also has the Composio-on-demand YouTube route. It is represented as a
discovery-gated source: Pulse must first obtain the exact read-only slugs and
input schemas through composio_discover, then use only a discovered read tool.
This package does not invoke that route or guess its slugs.

gemini_contract.py validates semantic completeness and evidence provenance.
It distinguishes VALID, PARTIAL, and INVALID, and separates
TRANSCRIPT_EVIDENCE, VISUAL_EVIDENCE, MODEL_INFERENCE, and UNVERIFIED.
Nested claim objects have explicit properties and cannot be empty objects;
timestamps without numeric values must carry an explicit unavailable state.

gemini_client.py is an isolated, one-shot Interactions API client. Its short
probe uses a bounded audiovisual schema, while `build_master_prompt_request()`
uses the complete JSON contract and `analyze_master_markdown()` uses the exact
versioned master prompt with a text/plain response and no JSON schema. Both
paths disable tools, storage, background execution, retries, and continuations.
The Markdown adapter preserves the provider text byte-for-byte and returns
detected sections, evidence references, unverified commands, completeness, and
validation errors without inventing technical facts.

The real Phase 3C.2 Markdown is preserved under `artifacts/` with a separate
metadata manifest and SHA-256. `pulse_interface.py` prepares a side-effect-free
Gemini-to-Pulse envelope containing independent extraction, audiovisual,
document-validation, and delivery states. It passes only a generated document
reference and hash; it rejects arbitrary paths and does not embed the full
Markdown or transcript in the envelope.

The default output budget is 32,768 tokens so the master prompt is not
constrained by the 2,048-token probe budget used in Phase 3B. The real call
path is never automatic and remains outside the MAX -> Pulse production route.

No real video URL is selected or called by this package automatically. A real
technical-video test requires an explicit caller decision and remains outside
the MAX -> Pulse production route.

## Fase 3E: adaptador aislado para Pulse

`pulse_adapter.py` prepara la integración opcional para la única combinación
explícita `kind=video.youtube.analyze` y
`analysis_type=video2implementation`. Las solicitudes `summary`, `transcript`,
`metadata` y `pulse.social` devuelven `None` al adaptador y conservan sus rutas
actuales.

La capacidad se controla con `VIDEO2IMPLEMENTATION_GEMINI_ENABLED`. La ausencia
de la variable, cualquier valor distinto de `1`, `true`, `yes` u `on`, y la
presencia de credenciales por sí sola mantienen la capacidad desactivada. Con
el flag apagado no se carga el prompt, no se crea un artefacto y no se llama al
proveedor; se devuelve un envelope compacto con estado `not_requested` y la
limitación `video2implementation_gemini_disabled`.

Con el flag activado en una futura fase, `Video2ImplementationPulseAdapter`
carga el prompt v3 controlado, usa `GeminiVideoClient` una sola vez, valida el
Markdown sin reescribirlo y lo entrega a `ControlledArtifactStore`. Este último
genera el nombre a partir de un `analysis_id` de la aplicación, rechaza
traversal, symlinks y sobrescrituras, y devuelve SHA-256 y referencia relativa.
El envelope contiene sólo estados independientes, resumen, errores,
limitaciones y referencia/hash del artefacto; nunca el Markdown ni el
transcript completo.

La Fase 3E no modifica `bridge.mjs`, MAX, Pulse productivo, `pulse.social` ni
los servicios systemd. La conexión de este adaptador al dispatcher de Pulse,
la activación del flag y la primera invocación real de Gemini requieren una
fase posterior con autorización explícita.


## Public source package

This folder contains the active Python implementation and prompt templates. Runtime artifacts, generated DOCX/PDF/Markdown outputs, cached media, logs, and historical snapshots are excluded. Configure the required model/API credentials in the local environment before enabling the workflow.
