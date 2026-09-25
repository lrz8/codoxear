"""Behavioral coverage for the input-modality focus-ring writer.

Ring visibility for script-driven focus is owned by one controller:
``createInputModalityController`` (``app_modal.js``) is the sole writer of
``<html data-input-modality>``. These tests execute the real module in a Node
VM and assert the attribute transitions plus the retained confirmation-dialog
focus contract (the default button stays focused; the CSS layer, not the
dialog, decides whether that focus rings).
"""

from __future__ import annotations

import json
import os
import subprocess
import textwrap
from pathlib import Path

from frontend_module_loader import module_path

ROOT = Path(__file__).resolve().parents[1]
APP_MODAL_JS = module_path("app_modal.js")


def _node_program(template: str) -> str:
    """Inject the VM fixture into a plain (non-f-string) JS template."""
    return textwrap.dedent(template).replace(
        "__MODAL_SOURCE__", json.dumps(APP_MODAL_JS.read_text(encoding="utf-8"))
    )


def _run_node(script: str) -> dict:
    proc = subprocess.run(
        ["node", "-e", script],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={"PATH": os.environ.get("PATH", ""), "TZ": "UTC"},
    )
    return json.loads(proc.stdout)


MODALITY_TEST = """
    const vm = require("vm");
    const modalSource = __MODAL_SOURCE__;

    const rootElement = {
      attributes: {},
      setAttribute(name, value) { this.attributes[name] = String(value); },
      getAttribute(name) {
        return Object.prototype.hasOwnProperty.call(this.attributes, name) ? this.attributes[name] : null;
      },
      style: {},
    };
    const documentTarget = { name: "document" };
    const registrations = [];
    function addAppEvent(target, type, handler, options) {
      registrations.push({ target, type, handler, options: options || null });
      return handler;
    }

    const ctx = { window: {} };
    vm.createContext(ctx);
    vm.runInContext(modalSource, ctx);
    const controller = ctx.window.CodoxearModal.createInputModalityController({
      documentTarget,
      rootElement,
      addAppEvent,
    });

    const dispatch = (type) => {
      for (const entry of registrations) {
        if (entry.type === type) entry.handler({ type });
      }
    };

    const beforeDispatch = rootElement.getAttribute("data-input-modality");
    dispatch("keydown");
    const afterKeydown = rootElement.getAttribute("data-input-modality");
    const modalityAfterKeydown = controller.modality();
    dispatch("pointerdown");
    const afterPointerdown = rootElement.getAttribute("data-input-modality");
    const modalityAfterPointerdown = controller.modality();
    dispatch("click");
    dispatch("focus");
    const afterUntouchedEvents = rootElement.getAttribute("data-input-modality");

    process.stdout.write(JSON.stringify({
      beforeDispatch,
      afterKeydown,
      modalityAfterKeydown,
      afterPointerdown,
      modalityAfterPointerdown,
      afterUntouchedEvents,
      registrations: registrations.map((entry) => ({
        target: entry.target === documentTarget,
        type: entry.type,
        capture: Boolean(entry.options && entry.options.capture === true),
        otherOptions: entry.options ? Object.keys(entry.options).filter((key) => key !== "capture") : [],
      })),
      surfaceKeys: Object.keys(controller),
      frozen: Object.isFrozen(controller),
    }));
"""


def test_modality_controller_transitions_and_single_writer_surface() -> None:
    result = _run_node(_node_program(MODALITY_TEST))

    # Initial value: "pointer" — nothing rings until the keyboard is used.
    assert result["beforeDispatch"] == "pointer"
    assert result["modalityAfterKeydown"] == result["afterKeydown"] == "keyboard"
    assert result["modalityAfterPointerdown"] == result["afterPointerdown"] == "pointer"
    # Events the controller did not register for cannot move the attribute.
    assert result["afterUntouchedEvents"] == "pointer"

    # Exactly one writer: two capture-phase document listeners, nothing else.
    registrations = result["registrations"]
    assert [(entry["type"], entry["capture"]) for entry in registrations] == [
        ("keydown", True),
        ("pointerdown", True),
    ]
    assert all(entry["target"] is True for entry in registrations)
    assert all(entry["otherOptions"] == [] for entry in registrations)

    # The controller surface exposes no second writer — only the reader.
    assert result["surfaceKeys"] == ["modality"]
    assert result["frozen"] is True


CONFIRMATION_TEST = """
    const vm = require("vm");
    const modalSource = __MODAL_SOURCE__;

    function button(name) {
      return {
        name,
        textContent: name,
        disabled: false,
        style: {},
        focused: 0,
        focus() { this.focused += 1; },
      };
    }
    const confirmButton = button("confirm");
    const cancelButton = button("cancel");
    const viewer = { style: { display: "none" }, classList: {} };
    const backdrop = { style: { display: "none" } };

    const ctx = { window: {} };
    vm.createContext(ctx);
    vm.runInContext(modalSource, ctx);
    const controller = ctx.window.CodoxearModal.createConfirmationController({
      backdrop,
      viewer,
      title: { textContent: "", style: {} },
      message: { textContent: "", style: {} },
      confirmButton,
      cancelButton,
      documentTarget: { activeElement: null },
      ElementCtor: Object,
      requestFrame: (callback) => callback(),
      prepareModalOpen: () => {},
      afterModalVisibilityChanged: () => {},
      addEvent: () => {},
    });

    const run = (destructive) => {
      confirmButton.focused = 0;
      cancelButton.focused = 0;
      const pending = controller.confirm({
        title: "Delete session?",
        message: "Delete?",
        confirmText: "Delete",
        cancelText: "Cancel",
        destructive,
      });
      const counts = {
        destructive,
        confirmFocused: confirmButton.focused,
        cancelFocused: cancelButton.focused,
      };
      return pending.then((confirmed) => Object.assign({}, counts, { confirmed }));
    };

    (async () => {
      const destructivePending = run(true);
      controller.resolve(false);
      const destructive = await destructivePending;
      const constructivePending = run(false);
      controller.resolve(false);
      const constructive = await constructivePending;
      process.stdout.write(JSON.stringify({ destructive, constructive }));
    })();
"""


def test_confirmation_dialog_still_focuses_its_default_button() -> None:
    result = _run_node(_node_program(CONFIRMATION_TEST))

    # focusInitial keeps focusing the default control: Cancel for destructive
    # dialogs (Delete stays one Tab away, never the landing focus), Confirm
    # otherwise. Ring visibility for that focus is decided by the modality
    # CSS, not by the dialog.
    destructive = result["destructive"]
    assert destructive["cancelFocused"] == 1
    assert destructive["confirmFocused"] == 0
    constructive = result["constructive"]
    assert constructive["confirmFocused"] == 1
    assert constructive["cancelFocused"] == 0
