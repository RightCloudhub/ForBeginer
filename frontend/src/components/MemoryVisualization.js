import React from 'react';
import './MemoryVisualization.css';

function MemoryVisualization({ stepData, supportsExecutionTrace, hasAnalysis }) {
  if (!hasAnalysis) {
    return (
      <div className="memory-empty-state">
        <h3>No run selected</h3>
        <p>Analyze a Python snippet to inspect its recorded values.</p>
      </div>
    );
  }

  if (!supportsExecutionTrace) {
    return (
      <div className="memory-empty-state">
        <h3>Trace unavailable</h3>
        <p>This run has no recorded values. Check the analysis notes for its execution capability.</p>
      </div>
    );
  }

  if (!stepData?.memory) {
    return (
      <div className="memory-empty-state">
        <h3>No step selected</h3>
        <p>Run a Python snippet and move through the trace to inspect stack and heap snapshots.</p>
      </div>
    );
  }

  const stackItems = stepData.memory.stack || [];
  const heapItems = stepData.memory.heap || [];

  return (
    <div className="memory-grid">
      <section className="memory-column">
        <header>
          <span>Local values</span>
          <strong>{stackItems.length}</strong>
        </header>
        <div className="memory-list">
          {stackItems.length === 0 && <MemoryEmpty label="No local values at this step." />}
          {stackItems.map((item) => (
            <article
              key={`${item.name}-${item.value}`}
              className={`memory-card memory-card-${item.type}`}
            >
              <div>
                <p>{item.name}</p>
                <span>{item.type}</span>
              </div>
              <code>{item.value}</code>
            </article>
          ))}
        </div>
      </section>

      <section className="memory-column">
        <header>
          <span>Referenced values</span>
          <strong>{heapItems.length}</strong>
        </header>
        <div className="memory-list">
          {heapItems.length === 0 && <MemoryEmpty label="No referenced values at this step." />}
          {heapItems.map((item) => (
            <article key={item.id} className="memory-card memory-card-reference">
              <div>
                <p>{item.id}</p>
                <span>reference</span>
              </div>
              <code>{item.value}</code>
            </article>
          ))}
        </div>
      </section>
    </div>
  );
}

function MemoryEmpty({ label }) {
  return (
    <div className="memory-empty-block">
      <p>{label}</p>
    </div>
  );
}

export default MemoryVisualization;
