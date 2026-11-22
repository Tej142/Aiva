"""Restricted AST mathematical evaluator and unit conversion tool without eval/exec."""

import ast
import math
import re
from typing import Any, Dict, Optional, Tuple, Union

# Whitelist of safe math functions
MATH_FUNCTIONS = {
    "sqrt": math.sqrt,
    "cbrt": getattr(math, "cbrt", lambda x: x ** (1.0 / 3.0)),
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "asin": math.asin,
    "acos": math.acos,
    "atan": math.atan,
    "sinh": math.sinh,
    "cosh": math.cosh,
    "tanh": math.tanh,
    "log": math.log,
    "log2": math.log2,
    "log10": math.log10,
    "exp": math.exp,
    "abs": abs,
    "round": round,
    "floor": math.floor,
    "ceil": math.ceil,
    "min": min,
    "max": max,
    "factorial": lambda x: math.factorial(int(x)) if 0 <= int(x) <= 100 else (_ for _ in ()).throw(ValueError("Factorial argument must be between 0 and 100")),
}

# Whitelist of mathematical constants
MATH_CONSTANTS = {
    "pi": math.pi,
    "e": math.e,
    "tau": math.tau,
}

# Unit conversion tables (base units: meter, gram, byte, m/s, second)
_LENGTH_UNITS = {
    "m": 1.0, "meter": 1.0, "meters": 1.0,
    "km": 1000.0, "kilometer": 1000.0, "kilometers": 1000.0,
    "cm": 0.01, "centimeter": 0.01, "centimeters": 0.01,
    "mm": 0.001, "millimeter": 0.001, "millimeters": 0.001,
    "mi": 1609.344, "mile": 1609.344, "miles": 1609.344,
    "yd": 0.9144, "yard": 0.9144, "yards": 0.9144,
    "ft": 0.3048, "foot": 0.3048, "feet": 0.3048,
    "in": 0.0254, "inch": 0.0254, "inches": 0.0254,
}

_MASS_UNITS = {
    "g": 1.0, "gram": 1.0, "grams": 1.0,
    "kg": 1000.0, "kilogram": 1000.0, "kilograms": 1000.0,
    "mg": 0.001, "milligram": 0.001, "milligrams": 0.001,
    "lb": 453.59237, "lbs": 453.59237, "pound": 453.59237, "pounds": 453.59237,
    "oz": 28.349523, "ounce": 28.349523, "ounces": 28.349523,
    "ton": 1_000_000.0, "tons": 1_000_000.0, "tonne": 1_000_000.0, "tonnes": 1_000_000.0,
}

_DATA_UNITS = {
    "b": 1.0, "byte": 1.0, "bytes": 1.0,
    "kb": 1024.0, "kilobyte": 1024.0, "kilobytes": 1024.0,
    "mb": 1024.0 ** 2, "megabyte": 1024.0 ** 2, "megabytes": 1024.0 ** 2,
    "gb": 1024.0 ** 3, "gigabyte": 1024.0 ** 3, "gigabytes": 1024.0 ** 3,
    "tb": 1024.0 ** 4, "terabyte": 1024.0 ** 4, "terabytes": 1024.0 ** 4,
}

_SPEED_UNITS = {
    "m/s": 1.0, "mps": 1.0,
    "km/h": 1.0 / 3.6, "kph": 1.0 / 3.6,
    "mph": 0.44704,
}

_TIME_UNITS = {
    "s": 1.0, "sec": 1.0, "second": 1.0, "seconds": 1.0,
    "min": 60.0, "minute": 60.0, "minutes": 60.0,
    "h": 3600.0, "hr": 3600.0, "hour": 3600.0, "hours": 3600.0,
    "day": 86400.0, "days": 86400.0,
    "week": 604800.0, "weeks": 604800.0,
}

_TEMP_UNITS = {"c", "celsius", "f", "fahrenheit", "k", "kelvin"}


class SafeMathEvaluator(ast.NodeVisitor):
    """AST visitor that evaluates only strictly whitelisted arithmetic nodes."""

    def visit_Expression(self, node: ast.Expression) -> Union[int, float]:
        return self.visit(node.body)

    def visit_Constant(self, node: ast.Constant) -> Union[int, float]:
        if isinstance(node.value, (int, float)):
            return node.value
        raise ValueError(f"Constant '{node.value}' of type {type(node.value).__name__} is not allowed.")

    def visit_Num(self, node: ast.Num) -> Union[int, float]:
        return node.n

    def visit_BinOp(self, node: ast.BinOp) -> Union[int, float]:
        left = self.visit(node.left)
        right = self.visit(node.right)
        op = node.op

        if isinstance(op, ast.Add):
            return left + right
        if isinstance(op, ast.Sub):
            return left - right
        if isinstance(op, ast.Mult):
            return left * right
        if isinstance(op, ast.Div):
            if right == 0:
                raise ZeroDivisionError("Division by zero")
            return left / right
        if isinstance(op, ast.FloorDiv):
            if right == 0:
                raise ZeroDivisionError("Division by zero")
            return left // right
        if isinstance(op, ast.Mod):
            if right == 0:
                raise ZeroDivisionError("Division by zero")
            return left % right
        if isinstance(op, ast.Pow):
            if abs(right) > 1000:
                raise ValueError("Exponent too large (maximum allowed exponent is 1000)")
            if abs(left) > 1_000_000 and abs(right) > 100:
                raise ValueError("Calculation exceeds safety bounds")
            return left ** right

        raise ValueError(f"Unsupported binary operator '{type(op).__name__}'.")

    def visit_UnaryOp(self, node: ast.UnaryOp) -> Union[int, float]:
        operand = self.visit(node.operand)
        if isinstance(node.op, ast.UAdd):
            return +operand
        if isinstance(node.op, ast.USub):
            return -operand
        raise ValueError(f"Unsupported unary operator '{type(node.op).__name__}'.")

    def visit_Call(self, node: ast.Call) -> Union[int, float]:
        if not isinstance(node.func, ast.Name):
            raise ValueError("Only direct function calls to approved math functions are permitted.")
        fn_name = node.func.id.lower()
        if fn_name not in MATH_FUNCTIONS:
            raise ValueError(f"Function '{fn_name}' is not permitted. Allowed: {sorted(MATH_FUNCTIONS.keys())}")
        if node.keywords:
            raise ValueError("Keyword arguments are not supported in math functions.")
        args = [self.visit(arg) for arg in node.args]
        return MATH_FUNCTIONS[fn_name](*args)

    def visit_Name(self, node: ast.Name) -> Union[int, float]:
        name = node.id.lower()
        if name in MATH_CONSTANTS:
            return MATH_CONSTANTS[name]
        raise ValueError(f"Unknown variable or identifier '{node.id}'.")

    def generic_visit(self, node: ast.AST) -> Any:
        raise ValueError(f"AST node '{type(node).__name__}' is forbidden for security.")


def _convert_temperature(val: float, src: str, dst: str) -> float:
    """Convert between Celsius, Fahrenheit, and Kelvin."""
    src = src.lower().rstrip("°")
    dst = dst.lower().rstrip("°")

    # Normalize to Celsius
    if src in ("c", "celsius"):
        c = val
    elif src in ("f", "fahrenheit"):
        c = (val - 32.0) * (5.0 / 9.0)
    elif src in ("k", "kelvin"):
        c = val - 273.15
    else:
        raise ValueError(f"Unknown temperature unit '{src}'.")

    # Convert from Celsius to target
    if dst in ("c", "celsius"):
        return c
    elif dst in ("f", "fahrenheit"):
        return (c * 9.0 / 5.0) + 32.0
    elif dst in ("k", "kelvin"):
        return c + 273.15
    else:
        raise ValueError(f"Unknown temperature unit '{dst}'.")


def _try_unit_conversion(expr: str) -> Optional[Dict[str, Any]]:
    """Check if the expression is a unit conversion query and evaluate it."""
    # Pattern: 100 km to miles, 32 fahrenheit in celsius, 5 kg as lbs
    pattern = r"^\s*([+-]?\d+(?:\.\d+)?)\s*([a-zA-Z°/]+)\s+(?:in|to|as)\s+([a-zA-Z°/]+)\s*$"
    m = re.match(pattern, expr.strip(), re.IGNORECASE)
    if not m:
        # Also check "convert 100 km to miles"
        pattern2 = r"^\s*convert\s+([+-]?\d+(?:\.\d+)?)\s*([a-zA-Z°/]+)\s+(?:in|to|as)\s+([a-zA-Z°/]+)\s*$"
        m = re.match(pattern2, expr.strip(), re.IGNORECASE)

    if not m:
        return None

    val = float(m.group(1))
    src = m.group(2).lower().rstrip("°")
    dst = m.group(3).lower().rstrip("°")

    # 1. Temperature
    if src in _TEMP_UNITS and dst in _TEMP_UNITS:
        res = _convert_temperature(val, src, dst)
        return {
            "status": "success",
            "type": "unit_conversion",
            "expression": expr,
            "value": val,
            "source_unit": src,
            "target_unit": dst,
            "result": round(res, 6),
            "formatted": f"{res:.4g} {dst}",
        }

    # 2. Ratio-based linear unit categories
    unit_tables = [
        ("length", _LENGTH_UNITS),
        ("mass", _MASS_UNITS),
        ("data", _DATA_UNITS),
        ("speed", _SPEED_UNITS),
        ("time", _TIME_UNITS),
    ]

    for cat_name, table in unit_tables:
        if src in table and dst in table:
            base_val = val * table[src]
            res = base_val / table[dst]
            return {
                "status": "success",
                "type": "unit_conversion",
                "category": cat_name,
                "expression": expr,
                "value": val,
                "source_unit": src,
                "target_unit": dst,
                "result": round(res, 6),
                "formatted": f"{res:.6g} {dst}",
            }

    # If units found in different tables or unknown
    return {
        "status": "error",
        "error": "incompatible_units",
        "message": f"Cannot convert between '{src}' and '{dst}'. Units must be in the same physical category.",
    }


def _preprocess_percentage(expr: str) -> str:
    """Preprocess natural percentage syntax like '15% of 80' or '20%' into standard arithmetic."""
    # Replace "X% of Y" with "((X / 100.0) * Y)"
    clean = re.sub(
        r"(\d+(?:\.\d+)?)\s*%\s*(?:of|\*)\s*(\d+(?:\.\d+)?)",
        r"((\1 / 100.0) * \2)",
        expr,
    )
    # Replace standalone "X%" (when NOT followed by an operand) with "(X / 100.0)"
    clean = re.sub(
        r"(\d+(?:\.\d+)?)\s*%(?!\s*[0-9a-zA-Z_])",
        r"(\1 / 100.0)",
        clean,
    )
    return clean


def safe_calculate(expression: str) -> Dict[str, Any]:
    """Safely calculate a mathematical expression or unit conversion without eval/exec."""
    raw = (expression or "").strip()
    if not raw:
        return {"status": "error", "error": "empty_expression", "message": "No expression provided."}

    # First check for unit conversion
    conv_res = _try_unit_conversion(raw)
    if conv_res is not None:
        return conv_res

    # Preprocess percentage
    processed = _preprocess_percentage(raw)

    try:
        parsed = ast.parse(processed, mode="eval")
    except SyntaxError as exc:
        return {
            "status": "error",
            "error": "syntax_error",
            "message": f"Syntax error in expression: {exc.msg}",
        }
    except Exception as exc:
        return {
            "status": "error",
            "error": "invalid_expression",
            "message": f"Invalid expression: {exc}",
        }

    evaluator = SafeMathEvaluator()
    try:
        result = evaluator.visit(parsed)
        if isinstance(result, float) and (math.isnan(result) or math.isinf(result)):
            return {
                "status": "error",
                "error": "math_domain_error",
                "message": "Calculation resulted in infinite or undefined value.",
            }
        # Format clean integer if exact whole number
        formatted_res: Union[int, float] = int(result) if isinstance(result, float) and result.is_integer() else round(result, 8) if isinstance(result, float) else result
        return {
            "status": "success",
            "type": "arithmetic",
            "expression": raw,
            "result": formatted_res,
            "formatted": str(formatted_res),
        }
    except ZeroDivisionError:
        return {
            "status": "error",
            "error": "zero_division",
            "message": "Cannot divide by zero.",
        }
    except (ValueError, TypeError) as exc:
        return {
            "status": "error",
            "error": "evaluation_error",
            "message": str(exc),
        }
    except Exception as exc:
        return {
            "status": "error",
            "error": "math_error",
            "message": f"Failed evaluating expression: {exc}",
        }
