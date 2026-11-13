import React, { StrictMode } from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import axios from 'axios';
import CodeMirror from 'codemirror';
import App from './App';

jest.mock('axios', () => ({
  post: jest.fn(),
}));

jest.mock('codemirror/mode/python/python', () => ({}));
jest.mock('codemirror/mode/clike/clike', () => ({}));

jest.mock('codemirror', () => {
  return {
    __esModule: true,
    default: jest.fn((mount, options) => {
      let value = options.value;
      const handlers = {};
      const markers = new Map();
      const wrapper = global.document.createElement('div');
      wrapper.className = 'CodeMirror';
      const input = global.document.createElement('textarea');
      input.setAttribute('aria-label', options.screenReaderLabel || 'Snippet code');
      input.value = value;
      wrapper.appendChild(input);
      mount.appendChild(wrapper);
      const editor = {
        on: jest.fn((event, handler) => { handlers[event] = handler; }),
        off: jest.fn((event) => { delete handlers[event]; }),
        setOption: jest.fn(),
        getValue: jest.fn(() => value),
        setValue: jest.fn((nextValue) => {
          value = nextValue;
          input.value = nextValue;
          handlers.change?.(editor, { origin: 'setValue' });
        }),
        getCursor: jest.fn(() => ({ line: 0, ch: 0 })),
        setCursor: jest.fn(),
        lineCount: jest.fn(() => value.split('\n').length),
        setGutterMarker: jest.fn((line, gutter, marker) => {
          if (marker) markers.set(line, marker);
          else markers.delete(line);
        }),
        lineInfo: jest.fn((line) => ({
          gutterMarkers: markers.has(line) ? { breakpoints: markers.get(line) } : {},
        })),
        getWrapperElement: jest.fn(() => wrapper),
        getInputField: jest.fn(() => input),
        addLineClass: jest.fn((line) => line),
        removeLineClass: jest.fn(),
        scrollIntoView: jest.fn(),
        operation: jest.fn((callback) => callback()),
        emit: (event, ...args) => handlers[event]?.(editor, ...args),
      };
      input.addEventListener('input', () => {
        value = input.value;
        handlers.change?.(editor, { origin: '+input' });
      });
      return editor;
    }),
  };
});

const createEditorMock = CodeMirror.getMockImplementation();

function traceResult(overrides = {}) {
  return {
    status: 'success',
    language: 'python',
    execution_trace: [
      { line: 1, variables: {}, memory: { stack: [], heap: [] } },
      {
        line: 2,
        variables: { value: '1' },
        memory: { stack: [{ name: 'value', value: '1', type: 'primitive' }], heap: [] },
      },
      {
        line: 3,
        variables: { value: '2' },
        memory: { stack: [{ name: 'value', value: '2', type: 'primitive' }], heap: [] },
      },
    ],
    metadata: { supports_execution_trace: true, breakpoints: [] },
    summary: { step_count: 3, executed_line_count: 3, final_variables: { value: '2' } },
    code_structure: { functions: [], loops: [], classes: [], conditionals: [] },
    stdout: '2\n',
    stderr: '',
    breakpoint_hits: [],
    paused_at: 0,
    ...overrides,
  };
}

function currentEditor() {
  return CodeMirror.mock.results[CodeMirror.mock.results.length - 1].value;
}

beforeEach(() => {
  axios.post.mockReset();
  CodeMirror.mockClear();
  CodeMirror.mockImplementation(createEditorMock);
});

test('renders an accessible code workspace', () => {
  render(<App />);

  expect(screen.getByText('CodeScope Studio')).toBeTruthy();
  expect(screen.getByRole('button', { name: /analyze code/i })).toBeTruthy();
  expect(screen.getByRole('textbox', { name: 'Snippet code' })).toBeTruthy();
});

test('editing reuses one editor instance and keeps its contents', () => {
  render(<App />);
  fireEvent.input(screen.getByRole('textbox'), { target: { value: 'value = 3\n' } });

  expect(CodeMirror).toHaveBeenCalledTimes(1);
  expect(screen.getAllByRole('textbox')).toHaveLength(1);
  expect(currentEditor().getValue()).toBe('value = 3\n');
});

test('StrictMode and unmount remove editor instances and handlers', () => {
  const { unmount } = render(<StrictMode><App /></StrictMode>);
  expect(screen.getAllByRole('textbox')).toHaveLength(1);

  const editor = currentEditor();
  unmount();
  expect(editor.off).toHaveBeenCalledWith('change', expect.any(Function));
  expect(editor.getWrapperElement().isConnected).toBe(false);
});

test('analysis sends edited code and exposes recorded trace navigation', async () => {
  axios.post.mockResolvedValue({ data: traceResult() });
  render(<App />);
  fireEvent.input(screen.getByRole('textbox'), {
    target: { value: 'value = 1\nvalue = 2\nprint(value)\n' },
  });
  fireEvent.click(screen.getByRole('button', { name: /analyze code/i }));

  await screen.findByRole('slider', { name: /execution step/i });
  expect(axios.post).toHaveBeenCalledWith(
    '/api/analyze',
    {
      code: 'value = 1\nvalue = 2\nprint(value)\n',
      language: 'python',
      debug: false,
      breakpoints: [],
    },
    expect.objectContaining({ signal: expect.anything(), timeout: 30000 })
  );
  expect(screen.getByText('Recorded')).toBeTruthy();
  expect(screen.getByText('2', { selector: 'pre' })).toBeTruthy();

  fireEvent.click(screen.getByRole('button', { name: /next step/i }));
  expect(screen.getByText('Step 2 / 3')).toBeTruthy();
  expect(currentEditor().addLineClass).toHaveBeenLastCalledWith(
    1, 'background', 'execution-line'
  );

  fireEvent.change(screen.getByRole('slider'), { target: { value: '2' } });
  expect(screen.getByText('Step 3 / 3')).toBeTruthy();
});

test('gutter breakpoints use one-based lines and continue through recorded hits', async () => {
  axios.post.mockResolvedValue({
    data: traceResult({ paused_at: 1, breakpoint_hits: [1, 2] }),
  });
  render(<App />);
  act(() => currentEditor().emit('gutterClick', 1, 'breakpoints'));
  act(() => currentEditor().emit('gutterClick', 2, 'breakpoints'));
  fireEvent.click(screen.getByRole('button', { name: /debug run/i }));

  await screen.findByText('Step 2 / 3');
  expect(axios.post.mock.calls[0][1].breakpoints).toEqual([2, 3]);
  fireEvent.click(screen.getByRole('button', { name: /continue/i }));
  expect(screen.getByText('Step 3 / 3')).toBeTruthy();
  expect(screen.getByRole('button', { name: /continue/i }).disabled).toBe(true);
});

test('F9 toggles a breakpoint without using the pointer', () => {
  render(<App />);
  const keyboard = CodeMirror.mock.calls[0][1].extraKeys;
  act(() => keyboard.F9(currentEditor()));
  expect(screen.getByText('1 breakpoint')).toBeTruthy();
  act(() => keyboard.F9(currentEditor()));
  expect(screen.getByText('0 breakpoints')).toBeTruthy();
});

test('breakpoints added after a run change local replay navigation', async () => {
  axios.post.mockResolvedValue({
    data: traceResult({ paused_at: 0, breakpoint_hits: [0] }),
  });
  render(<App />);
  act(() => currentEditor().emit('gutterClick', 0, 'breakpoints'));
  fireEvent.click(screen.getByRole('button', { name: /debug run/i }));
  await screen.findByText('Step 1 / 3');

  act(() => currentEditor().emit('gutterClick', 1, 'breakpoints'));
  fireEvent.click(screen.getByRole('button', { name: /continue/i }));
  expect(screen.getByText('Step 2 / 3')).toBeTruthy();
  expect(axios.post).toHaveBeenCalledTimes(1);
});

test('switching language aborts a pending request and ignores its response', async () => {
  let resolve;
  axios.post.mockReturnValue(new Promise((done) => { resolve = done; }));
  render(<App />);
  fireEvent.click(screen.getByRole('button', { name: /analyze code/i }));
  const signal = axios.post.mock.calls[0][2]?.signal;
  fireEvent.change(screen.getByRole('combobox'), { target: { value: 'c' } });

  expect(signal?.aborted).toBe(true);
  await act(async () => { resolve({ data: traceResult({ stdout: 'late response' }) }); });
  expect(screen.queryByText('late response')).toBeNull();
  expect(screen.queryByRole('slider')).toBeNull();
  expect(currentEditor().getValue()).toContain('#include <stdio.h>');
});

test('editing invalidates a completed result', async () => {
  axios.post.mockResolvedValue({ data: traceResult() });
  render(<App />);
  fireEvent.click(screen.getByRole('button', { name: /analyze code/i }));
  await screen.findByRole('slider');

  fireEvent.input(screen.getByRole('textbox'), { target: { value: 'value = 99\n' } });
  expect(screen.queryByRole('slider')).toBeNull();
  expect(screen.queryByText('2', { selector: 'pre' })).toBeNull();
});

test('editing aborts an in-progress run', async () => {
  let resolve;
  axios.post.mockReturnValue(new Promise((done) => { resolve = done; }));
  render(<App />);
  fireEvent.click(screen.getByRole('button', { name: /analyze code/i }));
  const signal = axios.post.mock.calls[0][2]?.signal;
  fireEvent.input(screen.getByRole('textbox'), { target: { value: 'value = 99\n' } });

  expect(signal?.aborted).toBe(true);
  await act(async () => { resolve({ data: traceResult({ stdout: 'outdated' }) }); });
  expect(screen.queryByText('outdated')).toBeNull();
  expect(screen.getByRole('button', { name: /analyze code/i }).disabled).toBe(false);
});

test('static C analysis renders diagnostics and output without a trace slider', async () => {
  axios.post.mockResolvedValue({
    data: traceResult({
      execution_trace: [],
      metadata: { supports_execution_trace: false },
      stdout: 'factorial(5) = 120',
      note: 'C analysis includes structure and program output.',
    }),
  });
  render(<App />);
  fireEvent.change(screen.getByRole('combobox'), { target: { value: 'c' } });
  fireEvent.click(screen.getByRole('button', { name: /analyze code/i }));

  await screen.findByText('factorial(5) = 120', { selector: 'pre' });
  expect(screen.queryByRole('slider')).toBeNull();
  expect(screen.getByText('Trace unavailable')).toBeTruthy();
});

test('structured API failures are announced with their message', async () => {
  axios.post.mockRejectedValue({
    response: { data: { error: { code: 'code_required', message: 'Code is required.' } } },
  });
  render(<App />);
  fireEvent.click(screen.getByRole('button', { name: /analyze code/i }));

  const alert = await screen.findByRole('alert');
  expect(alert.textContent).toContain('Code is required.');
});

test('malformed successful responses are shown as failures', async () => {
  axios.post.mockResolvedValue({ data: null });
  render(<App />);
  fireEvent.click(screen.getByRole('button', { name: /analyze code/i }));

  await waitFor(() => expect(screen.getByRole('alert').textContent).toMatch(/invalid response/i));
});
