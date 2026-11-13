import React from 'react';
import './CodeFlowVisualization.css';

const KIND_META = {
  function: { label: 'Function', accent: 'flow-function' },
  class: { label: 'Class', accent: 'flow-class' },
  struct: { label: 'Struct', accent: 'flow-struct' },
  loop: { label: 'Loop', accent: 'flow-loop' },
  conditional: { label: 'Branch', accent: 'flow-conditional' },
};

function CodeFlowVisualization({ codeStructure, currentStep }) {
  const items = collectItems(codeStructure);
  const activeIndex = getActiveIndex(items, currentStep?.line);

  if (!items.length) {
    return (
      <div className="flow-empty-state">
        <h3>No structure detected</h3>
        <p>Run valid code to generate functions, branches, and loop markers.</p>
      </div>
    );
  }

  return (
    <div className="flow-list">
      {items.map((item, index) => {
        const meta = KIND_META[item.kind] || KIND_META.conditional;
        const isActive = index === activeIndex;

        return (
          <article
            key={`${item.kind}-${item.line}-${item.label}`}
            className={`flow-item ${meta.accent} ${isActive ? 'flow-item-active' : ''}`}
            aria-label={item.label}
            aria-current={isActive ? 'step' : undefined}
          >
            <div className="flow-marker">
              <span>{item.endLine > item.line ? `${item.line}–${item.endLine}` : item.line}</span>
            </div>
            <div className="flow-content">
              <div className="flow-header">
                <span className="flow-kind">{meta.label}</span>
                <strong>{item.label}</strong>
              </div>
              {item.meta && <p>{item.meta}</p>}
            </div>
          </article>
        );
      })}
    </div>
  );
}

function collectItems(codeStructure) {
  if (!codeStructure) {
    return [];
  }

  const functions = (codeStructure.functions || []).map((item) => ({
    kind: 'function',
    line: item.line,
    endLine: item.end_line || item.line,
    label: item.name,
    meta: item.args?.length
      ? `args: ${item.args.join(', ')}`
      : item.params?.length
        ? `params: ${item.params.join(', ')}`
        : item.return_type
          ? `returns ${item.return_type}`
          : 'callable block',
  }));

  const classes = (codeStructure.classes || []).map((item) => ({
    kind: 'class',
    line: item.line,
    endLine: item.end_line || item.line,
    label: item.name,
    meta: item.bases?.length ? `extends ${item.bases.join(', ')}` : 'type definition',
  }));

  const structs = (codeStructure.structs || []).map((item) => ({
    kind: 'struct',
    line: item.line,
    endLine: item.end_line || item.line,
    label: item.name,
    meta: 'C structure definition',
  }));

  const loops = (codeStructure.loops || []).map((item) => ({
    kind: 'loop',
    line: item.line,
    endLine: item.end_line || item.line,
    label: `${item.type?.replace('_', ' ') || 'for'} loop`,
    meta: 'iterative control flow',
  }));

  const conditionals = (codeStructure.conditionals || []).map((item) => ({
    kind: 'conditional',
    line: item.line,
    endLine: item.end_line || item.line,
    label: item.type?.replace('_', ' ') || 'conditional',
    meta: 'branching path',
  }));

  return [...functions, ...classes, ...structs, ...loops, ...conditionals].sort(
    (left, right) => left.line - right.line
  );
}

function getActiveIndex(items, currentLine) {
  if (!items.length || !currentLine) {
    return -1;
  }

  return items.reduce((bestIndex, item, index) => {
    if (currentLine < item.line || currentLine > item.endLine) {
      return bestIndex;
    }
    if (bestIndex === -1) {
      return index;
    }

    const currentSpan = item.endLine - item.line;
    const bestSpan = items[bestIndex].endLine - items[bestIndex].line;
    return currentSpan < bestSpan ? index : bestIndex;
  }, -1);
}

export default CodeFlowVisualization;
