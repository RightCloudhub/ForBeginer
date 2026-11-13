import React, { useCallback, useEffect, useRef, useState } from 'react';
import axios from 'axios';
import CodeMirror from 'codemirror';
import 'codemirror/lib/codemirror.css';
import 'codemirror/theme/material.css';
import 'codemirror/mode/python/python';
import 'codemirror/mode/clike/clike';
import './styles/AppShell.css';
import CodeFlowVisualization from './components/CodeFlowVisualization';
import MemoryVisualization from './components/MemoryVisualization';
import VariableTable from './components/VariableTable';

const API_BASE_URL =
  (process.env.REACT_APP_API_BASE_URL || '').replace(/\/+$/, '');
const configuredTimeout = Number(process.env.REACT_APP_API_TIMEOUT_MS || 30000);
const REQUEST_TIMEOUT_MS =
  Number.isFinite(configuredTimeout) && configuredTimeout > 0 ? configuredTimeout : 30000;

const EXAMPLES = {
  python: `def bubble_sort(values):
    n = len(values)
    for i in range(n):
        for j in range(0, n - i - 1):
            if values[j] > values[j + 1]:
                values[j], values[j + 1] = values[j + 1], values[j]

numbers = [64, 34, 25, 12, 22, 11, 90]
print("Before:", numbers)
bubble_sort(numbers)
print("After:", numbers)`,
  c: `#include <stdio.h>

int factorial(int value) {
    if (value <= 1) {
        return 1;
    }
    return value * factorial(value - 1);
}

int main(void) {
    int input = 5;
    int output = factorial(input);
    printf("factorial(%d) = %d\\n", input, output);
    return 0;
}`,
};

const STATUS_LABELS = {
  success: 'Success',
  runtime_error: 'Runtime Error',
  validation_error: 'Validation Error',
  error: 'Error',
};

function App() {
  const [language, setLanguage] = useState('python');
  const [code, setCode] = useState(EXAMPLES.python);
  const [result, setResult] = useState(null);
  const [requestError, setRequestError] = useState('');
  const [loading, setLoading] = useState(false);
  const [currentStep, setCurrentStep] = useState(0);
  const [isDebugMode, setIsDebugMode] = useState(false);
  const [breakpoints, setBreakpoints] = useState([]);
  const editorMountRef = useRef(null);
  const editorRef = useRef(null);
  const codeRef = useRef(code);
  const languageRef = useRef(language);
  const highlightedLineRef = useRef(null);
  const requestRef = useRef(null);
  const requestVersionRef = useRef(0);

  const invalidateRun = useCallback(() => {
    requestVersionRef.current += 1;
    requestRef.current?.abort();
    requestRef.current = null;
    setLoading(false);
    setResult(null);
    setRequestError('');
    setCurrentStep(0);
  }, []);

  useEffect(() => {
    if (!editorMountRef.current || editorRef.current) {
      return undefined;
    }

    const toggleBreakpoint = (editor, lineNumber) => {
      if (languageRef.current !== 'python' || lineNumber < 0 || lineNumber >= editor.lineCount()) {
        return;
      }
      const active = Boolean(editor.lineInfo(lineNumber)?.gutterMarkers?.breakpoints);
      editor.setGutterMarker(
        lineNumber, 'breakpoints', active ? null : createBreakpointMarker()
      );
      setBreakpoints(readBreakpoints(editor));
    };

    const editor = CodeMirror(editorMountRef.current, {
      value: EXAMPLES.python,
      mode: 'python',
      theme: 'material',
      lineNumbers: true,
      lineWrapping: true,
      gutters: ['CodeMirror-linenumbers', 'breakpoints'],
      screenReaderLabel: 'Snippet code',
      extraKeys: {
        F9: (instance) => toggleBreakpoint(instance, instance.getCursor().line),
        Tab: false,
        'Shift-Tab': false,
      },
    });

    const handleChange = (instance) => {
      const nextValue = instance.getValue();
      if (codeRef.current === nextValue) {
        return;
      }
      codeRef.current = nextValue;
      setCode(nextValue);
      invalidateRun();
      const nextBreakpoints = readBreakpoints(instance);
      setBreakpoints((currentBreakpoints) => {
        return currentBreakpoints.join(',') === nextBreakpoints.join(',')
          ? currentBreakpoints : nextBreakpoints;
      });
    };

    const handleGutterClick = (instance, lineNumber, gutter) => {
      if (gutter === 'breakpoints' || gutter === 'CodeMirror-linenumbers') {
        toggleBreakpoint(instance, lineNumber);
      }
    };
    editor.on('change', handleChange);
    editor.on('gutterClick', handleGutterClick);
    editor.getInputField().id = 'snippet-input';
    editor.getInputField().setAttribute('aria-describedby', 'breakpoint-help');

    editorRef.current = editor;
    return () => {
      editor.off('change', handleChange);
      editor.off('gutterClick', handleGutterClick);
      editor.getWrapperElement().remove();
      editorRef.current = null;
      requestVersionRef.current += 1;
      requestRef.current?.abort();
      requestRef.current = null;
    };
  }, [invalidateRun]);

  useEffect(() => {
    const editor = editorRef.current;
    if (!editor) {
      return;
    }

    codeRef.current = code;
    if (editor.getValue() !== code) {
      const cursor = editor.getCursor();
      editor.setValue(code);
      editor.setCursor(cursor);
    }
  }, [code]);

  useEffect(() => {
    const editor = editorRef.current;
    if (!editor) {
      return;
    }

    languageRef.current = language;
    editor.setOption('mode', language === 'python' ? 'python' : 'text/x-csrc');
  }, [language]);

  useEffect(() => {
    const editor = editorRef.current;
    if (!editor) {
      return;
    }

    clearAllBreakpoints(editor);
    breakpoints.forEach((lineNumber) => {
      const zeroBasedLine = lineNumber - 1;
      if (zeroBasedLine >= 0 && zeroBasedLine < editor.lineCount()) {
        editor.setGutterMarker(zeroBasedLine, 'breakpoints', createBreakpointMarker());
      }
    });
  }, [breakpoints]);

  const executionTrace = result?.execution_trace || [];
  const totalSteps = executionTrace.length;
  const currentStepData =
    totalSteps > 0 && currentStep >= 0 && currentStep < totalSteps
      ? executionTrace[currentStep]
      : null;
  const breakpointHits = executionTrace.flatMap(
    (step, index) => breakpoints.includes(step.line) ? [index] : []
  );
  const canTrace = Boolean(result?.metadata?.supports_execution_trace && totalSteps > 0);
  const analysisState = getAnalysisState(result, requestError, loading);

  useEffect(() => {
    const editor = editorRef.current;
    if (!editor) {
      return;
    }
    if (highlightedLineRef.current !== null) {
      editor.removeLineClass(highlightedLineRef.current, 'background', 'execution-line');
      highlightedLineRef.current = null;
    }
    const line = currentStepData?.line;
    if (Number.isInteger(line) && line >= 1 && line <= editor.lineCount()) {
      highlightedLineRef.current = editor.addLineClass(
        line - 1, 'background', 'execution-line'
      );
      editor.scrollIntoView({ line: line - 1, ch: 0 }, 60);
    }
  }, [currentStepData, code]);

  const handleRun = async (debug) => {
    if (requestRef.current || !code.trim()) {
      return;
    }
    invalidateRun();
    const controller = new AbortController();
    requestRef.current = controller;
    const version = requestVersionRef.current;
    setLoading(true);
    setRequestError('');
    setResult(null);
    setCurrentStep(0);
    setIsDebugMode(debug);

    try {
      const response = await axios.post(
        `${API_BASE_URL}/api/analyze`,
        { code, language, debug, breakpoints },
        { signal: controller.signal, timeout: REQUEST_TIMEOUT_MS }
      );
      if (version !== requestVersionRef.current) {
        return;
      }

      const normalizedResult = normalizeResult(response.data, language);
      setResult(normalizedResult);
      setCurrentStep(resolveInitialStep(normalizedResult, debug));
    } catch (error) {
      if (version === requestVersionRef.current && !controller.signal.aborted) {
        setRequestError(extractRequestError(error));
      }
    } finally {
      if (version === requestVersionRef.current) {
        requestRef.current = null;
        setLoading(false);
      }
    }
  };

  const handleContinue = () => {
    if (!totalSteps) {
      return;
    }

    const nextBreakpoint = breakpointHits.find((stepIndex) => stepIndex > currentStep);
    if (typeof nextBreakpoint === 'number') {
      setCurrentStep(nextBreakpoint);
      return;
    }

    setCurrentStep(totalSteps - 1);
  };

  const handleResetExample = () => {
    invalidateRun();
    setCode(EXAMPLES[language]);
    setResult(null);
    setRequestError('');
    setCurrentStep(0);
    setBreakpoints([]);
    clearAllBreakpoints(editorRef.current);
  };

  const handleLanguageChange = (nextLanguage) => {
    invalidateRun();
    languageRef.current = nextLanguage;
    setLanguage(nextLanguage);
    setCode(EXAMPLES[nextLanguage]);
    setBreakpoints([]);
    setIsDebugMode(false);
    clearAllBreakpoints(editorRef.current);
  };

  return (
    <div className="app-shell">
      <a className="skip-link" href="#workspace">Skip to code workspace</a>
      <div className="app-background" aria-hidden="true" />
      <main className="app-layout" id="workspace" tabIndex={-1}>
        <section className="hero-panel">
          <div>
            <p className="eyebrow">CodeScope Studio</p>
            <h1>Code visualization for learners</h1>
            <p className="hero-copy">
              Trace Python variables and replay breakpoints. Check C structure,
              compilation, and program output.
            </p>
          </div>
          <div className="hero-meta">
            <div className="meta-card">
              <span>Languages</span>
              <strong>Python + C</strong>
            </div>
            <div className="meta-card">
              <span>Status</span>
              <strong>{analysisState.label}</strong>
            </div>
          </div>
        </section>

        <section className="workspace-grid">
          <div className="panel editor-panel">
            <div className="panel-header">
              <div>
                <p className="panel-kicker">Workspace</p>
                <h2>Snippet Editor</h2>
              </div>
              <div className="toolbar">
                <label htmlFor="language-select">Language</label>
                <select
                  id="language-select"
                  value={language}
                  onChange={(event) => handleLanguageChange(event.target.value)}
                >
                  <option value="python">Python</option>
                  <option value="c">C</option>
                </select>
              </div>
            </div>

            <div className="editor-actions">
              <button
                className="button button-primary"
                disabled={loading || !code.trim()}
                onClick={() => handleRun(false)}
              >
                {loading ? 'Running...' : 'Analyze code'}
              </button>
              <button
                className="button button-secondary"
                disabled={loading || !code.trim() || language !== 'python'}
                onClick={() => handleRun(true)}
                title="Replay Python breakpoints"
              >
                Debug Run
              </button>
              <button className="button button-ghost" onClick={handleResetExample}>
                Reload Example
              </button>
              <button
                className={`button ${isDebugMode ? 'button-secondary' : 'button-ghost'}`}
                onClick={() => setIsDebugMode((value) => !value)}
                aria-pressed={isDebugMode}
                disabled={language !== 'python'}
              >
                {isDebugMode ? 'Debug View On' : 'Enable Debug View'}
              </button>
            </div>

            <label className="editor-label" htmlFor="snippet-input">Snippet code</label>
            <div className="editor-frame">
              <div ref={editorMountRef} />
            </div>

            <div className="inline-note" id="breakpoint-help">
              <span>{breakpoints.length} {breakpoints.length === 1 ? 'breakpoint' : 'breakpoints'}</span>
              <span>Click a line number or press F9 to toggle a breakpoint.</span>
            </div>
          </div>

          <aside className="panel summary-panel">
            <div className="panel-header">
              <div>
                <p className="panel-kicker">Run Summary</p>
                <h2>Execution Overview</h2>
              </div>
              <span className={`status-badge status-${analysisState.tone}`} role="status">
                {analysisState.label}
              </span>
            </div>

            <div className="summary-grid">
              <SummaryCard
                label="Trace steps"
                value={String(result?.summary?.step_count || 0).padStart(2, '0')}
              />
              <SummaryCard
                label="Executed lines"
                value={String(result?.summary?.executed_line_count || 0).padStart(2, '0')}
              />
              <SummaryCard label="Breakpoints" value={String(breakpoints.length)} />
              <SummaryCard label="Trace mode" value={canTrace ? 'Recorded' : 'Static'} />
            </div>

            <div className="summary-block">
              <h3>Analysis notes</h3>
              <p>{result?.note || 'Run an analysis to inspect trace data, variables, and output.'}</p>
            </div>

            <div className="summary-block">
              <h3>Diagnostics</h3>
              <p>{analysisState.message}</p>
            </div>

            {result?.metadata?.compiler && (
              <div className="summary-block">
                <h3>Compiler</h3>
                <p>
                  {result.metadata.compiler.available
                    ? result.metadata.compiler.configured
                    : `${result.metadata.compiler.configured} unavailable`}
                </p>
              </div>
            )}
            {result?.metadata?.final_variables_available && (
              <details className="final-values">
                <summary>Final module values</summary>
                <VariableTable stepData={{ variables: result.summary.final_variables }} />
              </details>
            )}
          </aside>
        </section>

        {(requestError || result) && (
          <section className="feedback-strip">
            {requestError && <div className="feedback-card feedback-error" role="alert">{requestError}</div>}
            {!requestError && result?.error && (
              <div className="feedback-card feedback-warning" role="alert">{result.error}</div>
            )}
            {!requestError && !result?.error && result?.note && (
              <div className="feedback-card feedback-neutral">{result.note}</div>
            )}
          </section>
        )}

        {canTrace && (
          <section className="panel step-panel">
            <div className="panel-header">
              <div>
                <p className="panel-kicker">Debugger</p>
                <h2>Trace Navigator</h2>
              </div>
              <span className="step-indicator">
                Step {Math.min(currentStep + 1, totalSteps)} / {totalSteps}
              </span>
            </div>

            <div className="step-controls">
              <button
                className="button button-ghost"
                disabled={currentStep <= 0}
                onClick={() => setCurrentStep(0)}
              >
                First step
              </button>
              <button
                className="button button-ghost"
                disabled={currentStep <= 0}
                onClick={() => setCurrentStep((value) => Math.max(value - 1, 0))}
              >
                Previous step
              </button>
              <button
                className="button button-secondary"
                disabled={!isDebugMode || currentStep >= totalSteps - 1}
                onClick={handleContinue}
              >
                Continue
              </button>
              <button
                className="button button-ghost"
                disabled={currentStep >= totalSteps - 1}
                onClick={() =>
                  setCurrentStep((value) => Math.min(value + 1, totalSteps - 1))
                }
              >
                Next step
              </button>
              <button
                className="button button-ghost"
                disabled={currentStep >= totalSteps - 1}
                onClick={() => setCurrentStep(totalSteps - 1)}
              >
                Last step
              </button>
            </div>

            <label className="slider-label" htmlFor="execution-step">Execution step</label>
            <input
              id="execution-step"
              className="step-slider"
              type="range"
              min="0"
              max={Math.max(totalSteps - 1, 0)}
              value={currentStep}
              onChange={(event) => setCurrentStep(Number(event.target.value))}
            />

            <div className="inline-note">
              <span>Current line: {currentStepData?.line || '--'}</span>
              <span>
                Breakpoint hits: {breakpointHits.length ? breakpointHits.length : 'none'}
              </span>
            </div>
          </section>
        )}

        <section className="result-grid">
          <div className="panel">
            <div className="panel-header">
              <div>
                <p className="panel-kicker">Structure</p>
                <h2>Control Flow Map</h2>
              </div>
            </div>
            <CodeFlowVisualization
              codeStructure={result?.code_structure}
              currentStep={currentStepData}
              language={language}
            />
          </div>

          <div className="panel">
            <div className="panel-header">
              <div>
                <p className="panel-kicker">Memory</p>
                <h2>Value snapshots</h2>
              </div>
            </div>
            <MemoryVisualization
              stepData={currentStepData}
              hasAnalysis={Boolean(result)}
              supportsExecutionTrace={Boolean(result?.metadata?.supports_execution_trace)}
            />
          </div>
        </section>

        <section className="result-grid result-grid-lower">
          <div className="panel">
            <div className="panel-header">
              <div>
                <p className="panel-kicker">Variables</p>
                <h2>Observed Values</h2>
              </div>
            </div>
            <VariableTable stepData={currentStepData} />
          </div>

          <div className="panel">
            <div className="panel-header">
              <div>
                <p className="panel-kicker">Console</p>
                <h2>Program Output</h2>
              </div>
            </div>
            <ConsoleBlock title="stdout" value={result?.stdout} />
            <ConsoleBlock title="stderr" value={result?.stderr} />
          </div>
        </section>
      </main>
    </div>
  );
}

function SummaryCard({ label, value }) {
  return (
    <div className="summary-card">
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function ConsoleBlock({ title, value }) {
  return (
    <div className="console-block">
      <div className="console-header">{title}</div>
      <pre>{value || 'No output.'}</pre>
    </div>
  );
}

function normalizeResult(data, language) {
  if (!data || typeof data !== 'object' || Array.isArray(data) ||
      !Object.prototype.hasOwnProperty.call(STATUS_LABELS, data.status)) {
    throw new Error('Analysis service returned an invalid response.');
  }
  const executionTrace = Array.isArray(data?.execution_trace) ? data.execution_trace : [];
  if (!Array.isArray(data.execution_trace) || executionTrace.some(
    (step) => !step || typeof step !== 'object' || !Number.isInteger(step.line) ||
      !step.variables || typeof step.variables !== 'object' ||
      Array.isArray(step.variables) ||
      Object.values(step.variables).some((value) => typeof value !== 'string')
  )) {
    throw new Error('Analysis service returned an invalid response.');
  }
  const summary = data?.summary || {
    step_count: executionTrace.length,
    executed_line_count: 0,
    executed_lines: [],
    final_variables: {},
  };

  return {
    ...data,
    error: typeof data.error === 'string' ? data.error : data.error?.message || null,
    note: typeof data.note === 'string' ? data.note : null,
    language,
    execution_trace: executionTrace,
    breakpoint_hits: Array.isArray(data?.breakpoint_hits)
      ? data.breakpoint_hits.filter((index) => Number.isInteger(index) && index >= 0 && index < executionTrace.length)
      : [],
    paused_at: Number.isInteger(data?.paused_at) &&
      data.paused_at >= 0 && data.paused_at < executionTrace.length ? data.paused_at : null,
    metadata: data?.metadata || { supports_execution_trace: false, breakpoints: [] },
    summary,
  };
}

function resolveInitialStep(result, debug) {
  if (!result.execution_trace.length) {
    return 0;
  }
  if (debug && Number.isInteger(result.paused_at)) {
    return result.paused_at;
  }
  return 0;
}

function getAnalysisState(result, requestError, loading) {
  if (loading) {
    return {
      tone: 'running',
      label: 'Running',
      message: 'Analyzing the snippet.',
    };
  }
  if (requestError) {
    return {
      tone: 'error',
      label: 'Request Failed',
      message: requestError,
    };
  }

  if (!result) {
    return {
      tone: 'idle',
      label: 'Idle',
      message: 'No analysis has been executed yet.',
    };
  }

  const label = STATUS_LABELS[result.status] || 'Unknown';
  const tone = result.status === 'success' ? 'success' : result.status === 'runtime_error' ? 'warning' : 'error';
  const message =
    result.error ||
    result.note ||
    `${label}. ${result.metadata?.supports_execution_trace ? 'Trace data available.' : 'Static analysis only.'}`;

  return { tone, label, message };
}

function extractRequestError(error) {
  const payload = error?.response?.data;
  if (typeof payload?.error === 'string') {
    return payload.error;
  }
  if (payload?.error?.message) {
    return payload.error.message;
  }
  return error?.message || 'Unable to reach the analysis service.';
}

function createBreakpointMarker() {
  const marker = document.createElement('span');
  marker.className = 'breakpoint-marker';
  marker.setAttribute('aria-hidden', 'true');
  return marker;
}

function clearAllBreakpoints(editor) {
  if (!editor) {
    return;
  }

  const lineCount = editor.lineCount();
  for (let index = 0; index < lineCount; index += 1) {
    editor.setGutterMarker(index, 'breakpoints', null);
  }
}

function readBreakpoints(editor) {
  const lines = [];
  for (let index = 0; index < editor.lineCount(); index += 1) {
    if (editor.lineInfo(index)?.gutterMarkers?.breakpoints) {
      lines.push(index + 1);
    }
  }
  return lines;
}

export default App;
