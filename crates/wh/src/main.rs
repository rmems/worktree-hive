use std::io::{self, Write};
use std::process::ExitCode;

use clap::Parser;

/// Manage isolated issue-to-PR and PR-babysit jobs.
#[derive(Debug, Parser)]
#[command(name = "wh", version, about, long_about = None)]
struct Cli {
    /// Emit the scaffold response as a v1 JSON envelope.
    #[arg(long)]
    json: bool,
}

fn main() -> ExitCode {
    let cli = Cli::parse();

    match run(cli, &mut io::stdout()) {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            let _ = writeln!(io::stderr(), "wh: {error}");
            ExitCode::FAILURE
        }
    }
}

fn run(cli: Cli, stdout: &mut impl Write) -> io::Result<()> {
    if cli.json {
        serde_json::to_writer(
            &mut *stdout,
            &wh_core::contract::Response::bootstrap_success(),
        )
        .map_err(io::Error::other)?;
        stdout.write_all(b"\n")?;
    }

    Ok(())
}

#[cfg(test)]
mod tests {
    use clap::CommandFactory;
    use std::str;

    use super::{Cli, run};

    #[test]
    fn command_definition_is_valid() {
        let command = Cli::command();

        assert_eq!(command.get_version(), Some(wh_core::VERSION));
        command.debug_assert();
    }

    #[test]
    fn json_mode_writes_v1_envelope_to_stdout() {
        let cli = Cli { json: true };
        let mut stdout = Vec::new();

        run(cli, &mut stdout).unwrap();

        assert_eq!(
            str::from_utf8(&stdout).unwrap(),
            "{\"ok\":true,\"schema_version\":1,\"command\":\"cli.bootstrap\",\"data\":{},\"error\":null}\n"
        );
    }

    #[test]
    fn default_mode_keeps_stdout_empty() {
        let cli = Cli { json: false };
        let mut stdout = Vec::new();

        run(cli, &mut stdout).unwrap();

        assert!(stdout.is_empty());
    }
}
