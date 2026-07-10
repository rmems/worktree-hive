use clap::Parser;

/// Manage isolated issue-to-PR and PR-babysit jobs.
#[derive(Debug, Parser)]
#[command(name = "wh", version, about, long_about = None)]
struct Cli;

fn main() {
    let _cli = Cli::parse();
}

#[cfg(test)]
mod tests {
    use clap::CommandFactory;

    use super::Cli;

    #[test]
    fn command_definition_is_valid() {
        let command = Cli::command();

        assert_eq!(command.get_version(), Some(wh_core::VERSION));
        command.debug_assert();
    }
}
