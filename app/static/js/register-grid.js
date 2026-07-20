/**
 * Generic spreadsheet-style register grid, backed by app/registers/router.py's
 * JSON API. Consumers call RegisterGrid.init(containerId, config) — see
 * app/templates/controls/list.html for the reference usage.
 */
(function (global) {
  "use strict";

  function showAlert(container, message, kind) {
    var alertBox = document.createElement("div");
    alertBox.className = "alert alert-" + (kind || "danger") + " alert-dismissible fade show mt-2";
    alertBox.setAttribute("role", "alert");
    alertBox.textContent = message;
    var closeBtn = document.createElement("button");
    closeBtn.type = "button";
    closeBtn.className = "btn-close";
    closeBtn.setAttribute("data-bs-dismiss", "alert");
    closeBtn.setAttribute("aria-label", "Close");
    alertBox.appendChild(closeBtn);
    container.prepend(alertBox);
  }

  // Shared polite live region so screen-reader users get a spoken
  // confirmation on successful edits/adds — Tabulator's own DOM updates
  // are otherwise silent (SC 4.1.3 Status Messages).
  function announce(container, message) {
    var region = container.querySelector("[data-register-grid-status]");
    if (!region) {
      region = document.createElement("div");
      region.setAttribute("data-register-grid-status", "true");
      region.setAttribute("aria-live", "polite");
      region.className = "visually-hidden";
      container.appendChild(region);
    }
    region.textContent = message;
  }

  function rowLabel(data) {
    return data.title || data.name || "row";
  }

  function errorMessage(payload) {
    if (!payload) return "Save failed.";
    if (typeof payload.detail === "string") return payload.detail;
    if (payload.detail && payload.detail.errors) {
      return Object.entries(payload.detail.errors)
        .map(function (pair) {
          return pair[0] + ": " + pair[1].join(", ");
        })
        .join("; ");
    }
    if (payload.detail && typeof payload.detail === "object") {
      return Object.entries(payload.detail)
        .map(function (pair) {
          return pair[0] + ": " + (Array.isArray(pair[1]) ? pair[1].join(", ") : pair[1]);
        })
        .join("; ");
    }
    return "Save failed.";
  }

  function jsonFetch(url, options) {
    return fetch(url, options).then(function (response) {
      if (response.status === 204) return { ok: true, status: 204, body: null };
      return response.json().then(
        function (body) {
          return { ok: response.ok, status: response.status, body: body };
        },
        function () {
          return { ok: response.ok, status: response.status, body: null };
        }
      );
    });
  }

  function init(containerId, config) {
    var container = document.getElementById(containerId);
    if (!container) return null;

    var csrfHeaders = { "Content-Type": "application/json", "X-CSRF-Token": config.csrfToken };
    var deletable = config.deletable !== false;
    var actionColumns = deletable
      ? [
          {
            title: "Actions",
            field: "_actions",
            headerSort: false,
            headerHozAlign: "center",
            width: 60,
            formatter: function (cell) {
              var label = "Delete " + rowLabel(cell.getRow().getData());
              return (
                '<button type="button" class="btn btn-sm btn-outline-danger" data-action="delete" aria-label="' +
                label.replace(/"/g, "&quot;") +
                '">Delete</button>'
              );
            },
            cellClick: function (e, cell) {
              var target = e.target.closest("[data-action='delete']");
              if (!target) return;
              var row = cell.getRow();
              var data = row.getData();
              if (!window.confirm("Delete " + rowLabel(data) + "?")) return;
              jsonFetch(config.apiBase + "/" + data.id, {
                method: "DELETE",
                headers: { "X-CSRF-Token": config.csrfToken },
              }).then(function (res) {
                if (res.ok) {
                  row.delete();
                  announce(container, rowLabel(data) + " deleted.");
                } else {
                  showAlert(container, errorMessage(res.body), "danger");
                }
              });
            },
          },
        ]
      : [];

    var table = new Tabulator("#" + containerId, {
      ajaxURL: config.listUrl || config.apiBase,
      ajaxConfig: "GET",
      layout: "fitColumns",
      placeholder: config.emptyMessage || "No rows yet.",
      selectableRows: true,
      columns: (config.columns || []).concat(actionColumns),
    });

    table.on("cellEdited", function (cell) {
      var row = cell.getRow();
      var data = row.getData();
      var field = cell.getField();
      var payload = {
        fields: {},
        expected_updated_at: data.updated_at,
      };
      payload.fields[field] = cell.getValue();
      jsonFetch(config.apiBase + "/" + data.id, {
        method: "PATCH",
        headers: csrfHeaders,
        body: JSON.stringify(payload),
      }).then(function (res) {
        if (res.ok) {
          row.update(res.body);
          announce(container, field + " saved for " + rowLabel(res.body) + ".");
        } else {
          cell.restoreOldValue();
          showAlert(container, errorMessage(res.body), "danger");
        }
      });
    });

    if (config.addRowButtonId) {
      var addButton = document.getElementById(config.addRowButtonId);
      if (addButton) {
        addButton.addEventListener("click", function () {
          jsonFetch(config.apiBase, {
            method: "POST",
            headers: csrfHeaders,
            body: JSON.stringify(config.newRowDefaults || {}),
          }).then(function (res) {
            if (res.ok) {
              table.addRow(res.body, true);
              announce(container, rowLabel(res.body) + " added.");
            } else {
              showAlert(container, errorMessage(res.body), "danger");
            }
          });
        });
      }
    }

    return table;
  }

  global.RegisterGrid = { init: init };
})(window);
