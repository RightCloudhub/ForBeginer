import React from 'react';
import { render, screen } from '@testing-library/react';
import '@testing-library/jest-dom';
import CodeFlowVisualization from './CodeFlowVisualization';

const structure = {
  functions: [{ name: 'compute', line: 1, end_line: 5, args: [] }],
  loops: [{ type: 'for', line: 2, end_line: 4 }],
  conditionals: [],
  classes: [],
};

test('highlights the innermost block containing the current line', () => {
  render(
    <CodeFlowVisualization codeStructure={structure} currentStep={{ line: 3 }} />
  );
  expect(screen.getByRole('article', { name: 'for loop' })).toHaveAttribute('aria-current', 'step');
});

test('does not highlight unrelated blocks after their end', () => {
  render(
    <CodeFlowVisualization codeStructure={structure} currentStep={{ line: 8 }} />
  );
  screen.getAllByRole('article').forEach((item) => {
    expect(item).not.toHaveAttribute('aria-current');
  });
});

test('shows source ranges in the structure list', () => {
  render(<CodeFlowVisualization codeStructure={structure} />);
  expect(screen.getByText('1–5')).toBeTruthy();
  expect(screen.getByText('2–4')).toBeTruthy();
});
