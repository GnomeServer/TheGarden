(() => {
  "use strict";

  const display = document.querySelector("#display");
  const keys = document.querySelector(".keys");
  const maxDigits = 16;

  let current = "0";
  let accumulator = null;
  let pendingOperator = null;
  let waitingForOperand = false;
  let lastOperator = null;
  let lastOperand = null;
  let justEvaluated = false;
  let hasError = false;

  function render() {
    display.textContent = current;
  }

  function clearAll() {
    current = "0";
    accumulator = null;
    pendingOperator = null;
    waitingForOperand = false;
    lastOperator = null;
    lastOperand = null;
    justEvaluated = false;
    hasError = false;
    render();
  }

  function formatNumber(value) {
    if (!Number.isFinite(value)) return null;
    const rounded = Number(value.toPrecision(12));
    if (!Number.isFinite(rounded)) return null;
    return String(Object.is(rounded, -0) ? 0 : rounded);
  }

  function showError() {
    current = "Error";
    accumulator = null;
    pendingOperator = null;
    waitingForOperand = false;
    lastOperator = null;
    lastOperand = null;
    justEvaluated = false;
    hasError = true;
    render();
  }

  function calculate(left, right, operator) {
    switch (operator) {
      case "+": return left + right;
      case "-": return left - right;
      case "*": return left * right;
      case "/": return right === 0 ? Number.NaN : left / right;
      case "%": return right === 0 ? Number.NaN : left % right;
      default: return Number.NaN;
    }
  }

  function resetAfterResult() {
    accumulator = null;
    pendingOperator = null;
    waitingForOperand = false;
    lastOperator = null;
    lastOperand = null;
    justEvaluated = false;
  }

  function inputDigit(digit) {
    if (hasError || justEvaluated) {
      resetAfterResult();
      current = digit;
      hasError = false;
    } else if (waitingForOperand) {
      current = digit;
      waitingForOperand = false;
    } else if (current === "0") {
      current = digit;
    } else if (current === "-0") {
      current = `-${digit}`;
    } else if (current.replace("-", "").length < maxDigits) {
      current += digit;
    }
    render();
  }

  function inputDecimal() {
    if (hasError || justEvaluated) {
      resetAfterResult();
      current = "0.";
      hasError = false;
    } else if (waitingForOperand) {
      current = "0.";
      waitingForOperand = false;
    } else if (!current.includes(".")) {
      current += ".";
    }
    render();
  }

  function chooseOperator(operator) {
    if (hasError) clearAll();
    const input = Number(current);

    if (pendingOperator !== null && !waitingForOperand) {
      const result = calculate(accumulator, input, pendingOperator);
      const formatted = formatNumber(result);
      if (formatted === null) {
        showError();
        return;
      }
      current = formatted;
      accumulator = result;
    } else if (accumulator === null || justEvaluated) {
      accumulator = input;
    }

    pendingOperator = operator;
    waitingForOperand = true;
    lastOperator = null;
    lastOperand = null;
    justEvaluated = false;
    render();
  }

  function equals() {
    if (hasError) return;

    if (pendingOperator !== null) {
      const left = accumulator === null ? Number(current) : accumulator;
      const right = waitingForOperand ? left : Number(current);
      const operator = pendingOperator;
      const result = calculate(left, right, operator);
      const formatted = formatNumber(result);
      if (formatted === null) {
        showError();
        return;
      }
      current = formatted;
      accumulator = null;
      pendingOperator = null;
      waitingForOperand = false;
      lastOperator = operator;
      lastOperand = right;
      justEvaluated = true;
      render();
      return;
    }

    if (lastOperator !== null) {
      const result = calculate(Number(current), lastOperand, lastOperator);
      const formatted = formatNumber(result);
      if (formatted === null) {
        showError();
        return;
      }
      current = formatted;
      justEvaluated = true;
      render();
    }
  }


  function backspace() {
    if (hasError) {
      clearAll();
      return;
    }
    if (waitingForOperand) return;
    if (justEvaluated) {
      lastOperator = null;
      lastOperand = null;
      justEvaluated = false;
    }
    if (current.length <= 1 || (current.length === 2 && current.startsWith("-"))) {
      current = "0";
    } else {
      current = current.slice(0, -1);
    }
    render();
  }

  function handleButton(button) {
    if (button.dataset.digit !== undefined) {
      inputDigit(button.dataset.digit);
    } else if (button.dataset.operator !== undefined) {
      chooseOperator(button.dataset.operator);
    } else {
      switch (button.dataset.action) {
        case "clear": clearAll(); break;
        case "backspace": backspace(); break;
        case "decimal": inputDecimal(); break;
        case "equals": equals(); break;
      }
    }
  }

  keys.addEventListener("click", (event) => {
    const button = event.target.closest("button");
    if (button && keys.contains(button)) handleButton(button);
  });

  document.addEventListener("keydown", (event) => {
    const key = event.key;
    if (/^\d$/.test(key)) {
      inputDigit(key);
    } else if (key === "." || key === ",") {
      inputDecimal();
    } else if (["+", "-", "*", "/", "%"].includes(key)) {
      chooseOperator(key);
    } else if (key === "x" || key === "X" || key === "×") {
      chooseOperator("*");
    } else if (key === "÷") {
      chooseOperator("/");
    } else if (key === "Enter" || key === "=") {
      equals();
    } else if (key === "Escape" || key === "Delete") {
      clearAll();
    } else if (key === "Backspace") {
      backspace();
    } else {
      return;
    }
    event.preventDefault();
  });

  render();
})();
