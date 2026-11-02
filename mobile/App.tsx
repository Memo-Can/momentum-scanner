import { useEffect, useRef } from 'react';
import { BackHandler, Platform, SafeAreaView, StatusBar, StyleSheet } from 'react-native';
import WebView from 'react-native-webview';

// Web dashboard'un canli adresi - mobil uygulama bunu bir WebView icinde
// aciyor. Web'de yapilan HER degisiklik (yeni sutun, ozellik, tasarim vb.)
// ayri bir mobil surum yayinlamaya gerek kalmadan otomatik olarak mobilde
// de gorunur - WebView yaklasiminin tum amaci bu.
const WEB_URL = 'https://momentumscanner.com.tr';

// Push bildirimi (expo-notifications/expo-device ile FCM device-token kaydi
// + bildirime dokunulunca hisseye deep-link) 2026-10'da KALDIRILDI - backend
// Firebase'den Telegram'a gecti (bkz. backend CLAUDE.md "Bildirimler"),
// `/api/register-device` endpoint'i silindi. Bu dosyadaki tum push akisi
// (izin isteme, token kaydi, bildirime-dokununca-ac) SADECE o FCM
// mekanizmasina bagliydi - backend karsiligi olmadan: (1) kullanicidan
// anlamsiz bir bildirim izni istiyordu, (2) her acilista var olmayan bir
// endpoint'e istek atiyordu, (3) hic gelmeyecek bir bildirime "dokununca ac"
// mantigi zaten tetiklenemiyordu. Hepsi kaldirildi - uygulama artik SADECE
// bir WebView sarmalayici, bildirimler tamamen Telegram uzerinden gidiyor.
export default function App() {
  const webViewRef = useRef<WebView>(null);

  // Android geri tusu: WebView icinde geri gidilecek sayfa varsa once onu
  // kullan, yoksa varsayilan davranisa (uygulamadan cik) birak.
  useEffect(() => {
    if (Platform.OS !== 'android') return;
    const sub = BackHandler.addEventListener('hardwareBackPress', () => {
      if (webViewRef.current) {
        webViewRef.current.injectJavaScript(
          'window.history.length > 1 && window.history.back(); true;'
        );
        return true;
      }
      return false;
    });
    return () => sub.remove();
  }, []);

  return (
    <SafeAreaView style={styles.container}>
      <StatusBar barStyle="light-content" backgroundColor="#0f1115" />
      <WebView ref={webViewRef} source={{ uri: WEB_URL }} style={styles.webview} />
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: '#0f1115' },
  webview: { flex: 1, backgroundColor: '#0f1115' },
});
