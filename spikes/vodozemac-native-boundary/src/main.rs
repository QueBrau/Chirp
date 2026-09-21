fn main() {
    for case in 0..9 {
        if chirp_native_boundary_probe::run_probe(case).is_err() {
            eprintln!("native_boundary_probe_failed: case {case}");
            std::process::exit(1);
        }
    }
    println!("{{\"native_cpp_cases_passed\":9,\"host_only\":true,\"expo_or_phone_proof\":false}}");
}
