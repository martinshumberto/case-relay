import { Component, type ErrorInfo, type ReactNode } from "react";

interface Props {
  children: ReactNode;
}

interface State {
  error: Error | null;
}

export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("unhandled render error", error, info);
  }

  render() {
    if (!this.state.error) return this.props.children;

    return (
      <div role="alert" style={{ padding: 16, border: "1px solid #c00", borderRadius: 6 }}>
        <h2 style={{ marginTop: 0 }}>Algo quebrou nesta tela</h2>
        <p style={{ fontFamily: "monospace", fontSize: 13 }}>{this.state.error.message}</p>
        <button onClick={() => this.setState({ error: null })}>Tentar novamente</button>
      </div>
    );
  }
}
