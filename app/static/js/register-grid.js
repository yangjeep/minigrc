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

    var table = new Tabulator("#" + containerId, {
      ajaxURL: config.apiBase,
      ajaxConfig: "GET",
      layout: "fitColumns",
      placeholder: config.emptyMessage || "No rows yet.",
      selectableRows: true,
      columns: (config.columns || []).concat([
        {
          title: "",
          field: "_actions",
          headerSort: false,
          width: 60,
          formatter: function () {
            return '<button type="button" class="btn btn-sm btn-outline-danger" data-action="delete">Delete</button>';
          },
          cellClick: function (e, cell) {
            var target = e.target.closest("[data-action='delete']");
            if (!target) return;
            var row = cell.getRow();
            var data = row.getData();
            if (!window.confirm("Delete this row?")) return;
            jsonFetch(config.apiBase + "/" + data.id, {
              method: "DELETE",
              headers: { "X-CSRF-Token": config.csrfToken },
            }).then(function (res) {
              if (res.ok) {
                row.delete();
              } else {
                showAlert(container, errorMessage(res.body), "danger");
              }
            });
          },
        },
      ]),
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
