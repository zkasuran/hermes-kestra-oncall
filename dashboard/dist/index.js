/* Kestra on-call: read-only dashboard tab.
 * Lists stored incidents with their proposed diff, and current Pause questions. It never writes to Kestra:
 * approval happens in the Hermes chat, where the stored diff and exact proposal hash are shown. */
(function () {
  "use strict";

  var SDK = window.__HERMES_PLUGIN_SDK__;
  var React = SDK.React;
  var useState = SDK.hooks.useState;
  var useEffect = SDK.hooks.useEffect;
  var useCallback = SDK.hooks.useCallback;
  var C = SDK.components;
  var h = React.createElement;
  var API = "/api/plugins/kestra-oncall/overview";
  var REFRESH_MS = 10000;

  function badge(text, tone) {
    return h("span", { className: "ko-badge ko-" + (tone || "muted") }, text);
  }

  function copy(text) {
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(text);
    } catch (e) {
      /* clipboard may be blocked; the command stays visible and selectable */
    }
  }

  function Command(props) {
    return h("div", { className: "ko-command" },
      h("code", { className: "ko-code" }, props.text),
      h(C.Button, { size: "sm", variant: "outline", onClick: function () { copy(props.text); } }, "Copy"));
  }

  function Diff(props) {
    var lines = (props.text || "").split("\n");
    return h("pre", { className: "ko-diff", "aria-label": "Proposed flow change" },
      lines.map(function (line, i) {
        var kind = "ctx";
        if (line.indexOf("+++") === 0 || line.indexOf("---") === 0) kind = "file";
        else if (line.indexOf("@@") === 0) kind = "hunk";
        else if (line.charAt(0) === "+") kind = "add";
        else if (line.charAt(0) === "-") kind = "del";
        return h("div", { key: i, className: "ko-line ko-line-" + kind }, line || " ");
      }));
  }

  function statusTone(status) {
    if (status === "verified") return "good";
    if (status === "rolled_back" || status === "pending" || status === "stale" || status === "conflict" || status === "needs_human") return "warn";
    if (status === "proposed") return "info";
    return "muted";
  }

  function Incident(props) {
    var inc = props.incident;
    var proposal = inc.proposal;
    return h(C.Card, { className: "ko-card" },
      h(C.CardHeader, null,
        h(C.CardTitle, { className: "ko-title" },
          h("span", null, inc.namespace + "." + inc.flow_id),
          badge(inc.status, statusTone(inc.status)),
          inc.protected ? badge("protected: diagnose only", "warn") : null)),
      h(C.CardContent, { className: "ko-body" },
        h("dl", { className: "ko-facts" },
          h("dt", null, "Incident"), h("dd", null, inc.incident_id),
          h("dt", null, "Failed task"), h("dd", null, inc.failed_task || "unknown"),
          h("dt", null, "Execution"), h("dd", null, inc.execution_id || "unknown"),
          inc.execution_revision != null ? h("dt", null, "Revision") : null,
          inc.execution_revision != null ? h("dd", null, String(inc.execution_revision)) : null),
        inc.result
          ? h("div", { className: "ko-result" },
              badge(inc.result.status, statusTone(inc.result.status)),
              h("span", null, inc.result.message),
              inc.result.fixed_revision != null ? h("span", { className: "ko-note" }, "fixed revision " + inc.result.fixed_revision) : null,
              inc.result.replay_execution_id ? h("span", { className: "ko-note" }, "replay " + inc.result.replay_execution_id) : null)
          : null,
        inc.error_log && inc.error_log.length
          ? h("pre", { className: "ko-log", "aria-label": "Error log" }, inc.error_log.join("\n"))
          : null,
        proposal
          ? h("div", { className: "ko-proposal" },
              h("div", { className: "ko-subhead" }, "Proposed fix",
                badge("base revision " + proposal.base_revision, "muted")),
              proposal.rationale ? h("p", { className: "ko-rationale" }, proposal.rationale) : null,
              h(Diff, { text: proposal.diff }),
              proposal.diff_truncated ? h("p", { className: "ko-note" }, "Diff shortened here. The chat approval shows the full stored diff.") : null,
              h("div", { className: "ko-hash" }, "SHA-256 ", h("code", null, proposal.sha256)),
              inc.apply_command ? h(Command, { text: inc.apply_command }) : null,
              h("p", { className: "ko-note" }, inc.apply_command
                ? "Type this command in the Hermes chat to approve exactly this diff. This page cannot write to Kestra."
                : (inc.apply_note || "No approval is pending for this incident.")))
          : h("p", { className: "ko-note" }, inc.protected
              ? "Protected flow: Hermes diagnoses it but never proposes or writes a fix."
              : "No proposal yet. Ask Hermes to inspect this incident and propose a fix.")));
  }

  function Pause(props) {
    var p = props.pause;
    return h(C.Card, { className: "ko-card" },
      h(C.CardHeader, null,
        h(C.CardTitle, { className: "ko-title" },
          h("span", null, p.namespace + "." + p.flow_id),
          badge("paused at " + (p.paused_task_id || "task"), "info"),
          p.protected ? badge("protected", "warn") : null)),
      h(C.CardContent, { className: "ko-body" },
        p.description ? h("p", { className: "ko-rationale" }, p.description) : null,
        h("dl", { className: "ko-facts" },
          h("dt", null, "Execution"), h("dd", null, p.execution_id),
          p.paused_for_seconds != null ? h("dt", null, "Waiting") : null,
          p.paused_for_seconds != null ? h("dd", null, Math.round(p.paused_for_seconds) + " s") : null),
        p.resume_inputs && p.resume_inputs.length
          ? h("div", { className: "ko-inputs" },
              h("div", { className: "ko-subhead" }, "Resume inputs"),
              p.resume_inputs.map(function (row) {
                return h("div", { key: row.name, className: "ko-input-row" },
                  h("code", null, row.name), row.type ? badge(row.type, "muted") : null,
                  row.required ? badge("required", "info") : null);
              }))
          : null,
        p.ambiguous
          ? h("p", { className: "ko-note" }, "More than one task run in this execution is paused. Kestra resumes an execution as a whole, so resume is refused here. Killing ends the whole execution.")
          : h(Command, { text: p.resume_command }),
        h(Command, { text: p.kill_command }),
        h("p", { className: "ko-note" }, "Type the command in the Hermes chat. Killing ends the execution.")));
  }

  function Section(props) {
    return h("section", { className: "ko-section" },
      h("h2", { className: "ko-h2" }, props.title, badge(String(props.count), props.count ? "info" : "muted")),
      props.children);
  }

  function Page() {
    var s = useState(null); var data = s[0], setData = s[1];
    var e = useState(null); var failure = e[0], setFailure = e[1];
    var a = useState(true); var auto = a[0], setAuto = a[1];

    var load = useCallback(function () {
      return SDK.fetchJSON(API).then(function (d) { setData(d); setFailure(null); })
        .catch(function (err) { setFailure(String((err && err.message) || err)); });
    }, []);

    useEffect(function () {
      load();
      if (!auto) return undefined;
      var timer = setInterval(load, REFRESH_MS);
      return function () { clearInterval(timer); };
    }, [auto, load]);

    var settings = (data && data.settings) || {};
    var errors = (data && data.errors) || {};
    var errorKeys = Object.keys(errors);
    var incidents = (data && data.incidents) || [];
    var pauses = (data && data.pauses) || [];

    return h("div", { className: "ko-page" },
      h("header", { className: "ko-header" },
        h("div", null,
          h("h1", { className: "ko-h1" }, "Kestra on-call"),
          h("div", { className: "ko-badges" },
            data ? badge(data.connected ? "Kestra " + data.version + " connected" : "Kestra unreachable", data.connected ? "good" : "bad") : badge("loading", "muted"),
            data ? badge(data.policy_ready ? settings.namespaces.length + " namespace" + (settings.namespaces.length === 1 ? "" : "s") + " allowed" : "no namespaces allowed", data.policy_ready ? "info" : "warn") : null,
            settings.tenant ? badge("tenant " + settings.tenant, "muted") : null,
            settings.rollback_on_failure ? badge("rollback on failure", "muted") : null,
            badge("read-only", "muted"))),
        h("div", { className: "ko-actions" },
          h(C.Button, { size: "sm", variant: "outline", onClick: function () { setAuto(!auto); } }, auto ? "Pause refresh" : "Resume refresh"),
          h(C.Button, { size: "sm", onClick: load }, "Refresh"))),
      failure ? h("div", { className: "ko-alert", role: "alert" }, "Could not load: " + failure) : null,
      errorKeys.map(function (key) { return h("div", { key: key, className: "ko-alert", role: "alert" }, key + ": " + errors[key]); }),
      data && !data.policy_ready
        ? h("div", { className: "ko-alert", role: "status" }, "No namespaces are allowed, so Hermes will not open incidents. Set plugins.entries.kestra-oncall.settings.namespaces, then run: hermes kestra-oncall doctor.")
        : null,
      h(Section, { title: "Incidents", count: incidents.length },
        incidents.length
          ? incidents.map(function (inc) { return h(Incident, { key: inc.incident_id, incident: inc }); })
          : h("p", { className: "ko-empty" }, data ? "No incidents. A failed execution in an allowed namespace appears here once Hermes opens it." : "Loading.")),
      h(Section, { title: "Pending pauses", count: pauses.length },
        pauses.length
          ? pauses.map(function (p) { return h(Pause, { key: p.execution_id + p.paused_task_run_id, pause: p }); })
          : h("p", { className: "ko-empty" }, data ? "No paused executions." : "Loading.")));
  }

  window.__HERMES_PLUGINS__.register("kestra-oncall", Page);
})();
