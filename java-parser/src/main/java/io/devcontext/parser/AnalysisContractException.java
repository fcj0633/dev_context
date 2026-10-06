package io.devcontext.parser;

/** Invalid analyzer output is fatal, unlike missing dependency coverage. */
public final class AnalysisContractException extends IllegalStateException {
    public AnalysisContractException(String message) { super(message); }
}
