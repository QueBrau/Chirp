require 'json'

# The iOS half of the E2EE core (board c448, design section 2): an Expo local module whose
# Swift sources wrap the Rust core. The core ships as a vendored static xcframework plus
# the uniffi-generated Swift, and both are BUILD ARTIFACTS of scripts/build-ios.sh (not in
# git). If they are missing this podspec raises, so `pod install` - and therefore
# `expo prebuild`, `expo run:ios` and an EAS build - stops here with the fix, rather than
# producing an app that fails to link or, worse, one that silently lacks the module.

framework = File.join(__dir__, 'Frameworks', 'ChirpCryptoCore.xcframework')
bindings = File.join(__dir__, 'Generated', 'chirp_crypto_core.swift')

unless File.directory?(framework) && File.file?(bindings)
  raise "ChirpCrypto: the Rust core has not been built for iOS.\n" \
        "  missing: #{File.directory?(framework) ? '' : framework} #{File.file?(bindings) ? '' : bindings}\n" \
        "  run:     app-mobile/modules/chirp-crypto/scripts/build-ios.sh\n" \
        "  (EAS builds run it from the eas-build-post-install hook in app-mobile/package.json)"
end

Pod::Spec.new do |s|
  s.name           = 'ChirpCrypto'
  s.version        = '0.1.0'
  s.summary        = 'Chirp end-to-end encrypted direct messages: Rust core behind an Expo module'
  s.description    = 'Wraps the vodozemac-based Rust crypto core (uniffi bindings) for the Chirp app. ' \
                     'Private keys and the store key never reach JavaScript.'
  s.author         = 'Chirp'
  s.homepage       = 'https://chirpsocials.com'
  s.license        = { :type => 'Proprietary', :text => 'Copyright Chirp. All rights reserved.' }
  s.platforms      = { :ios => '15.1' }
  s.swift_version  = '5.9'
  s.source         = { :git => 'https://github.com/QueBrau/Chirp.git' }
  s.static_framework = true

  s.dependency 'ExpoModulesCore'

  s.pod_target_xcconfig = {
    'DEFINES_MODULE' => 'YES',
    'SWIFT_COMPILATION_MODE' => 'wholemodule'
  }

  s.source_files = '*.swift', 'Generated/*.swift'
  s.vendored_frameworks = 'Frameworks/ChirpCryptoCore.xcframework'
end
