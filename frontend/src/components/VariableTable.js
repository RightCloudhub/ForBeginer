import React from 'react';
import './VariableTable.css';

function VariableTable({ stepData }) {
  const variables = Object.entries(stepData?.variables || {}).map(([name, value]) => ({
    name,
    value,
    type: getVariableType(value),
  }));

  if (!variables.length) {
    return (
      <div className="variable-table-empty">
        <h3>No variables captured</h3>
        <p>Advance through the trace to inspect local values for each executed line.</p>
      </div>
    );
  }

  return (
    <div className="variable-table-container">
      <table className="variable-table">
        <thead>
          <tr>
            <th>Name</th>
            <th>Type</th>
            <th>Value</th>
          </tr>
        </thead>
        <tbody>
          {variables.map((variable) => (
            <tr key={variable.name}>
              <td className="variable-name">{variable.name}</td>
              <td>
                <span className={`variable-pill variable-pill-${variable.type}`}>
                  {formatTypeLabel(variable.type, variable.value)}
                </span>
              </td>
              <td className="variable-value">{variable.value}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function getVariableType(value) {
  if (!value) {
    return 'primitive';
  }

  const cleanValue = value.trim();
  const isPrimitive =
    /^-?\d+(\.\d+)?$/.test(cleanValue) ||
    /^(True|False|None|null|true|false)$/.test(cleanValue) ||
    /^(['"]).*\1$/.test(cleanValue);

  return isPrimitive ? 'primitive' : 'reference';
}

function formatTypeLabel(type, value) {
  if (type === 'primitive') {
    if (/^-?\d+(\.\d+)?$/.test(value)) {
      return 'number';
    }
    if (/^(True|False|None|null|true|false)$/.test(value)) {
      return 'literal';
    }
    return 'scalar';
  }

  if (value.includes('[') || value.includes('list') || value.includes('tuple')) {
    return 'sequence';
  }
  if (value.includes('{') || value.includes('dict')) {
    return 'mapping';
  }
  return 'reference';
}

export default VariableTable;
