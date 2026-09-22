import { useState } from 'react';
import { KeyboardAvoidingView, Platform, Pressable, StyleSheet, Text, TextInput, View } from 'react-native';

import { Screen } from '@/components/Screen';
import { colors, radii, spacing } from '@/lib/theme';
import { supabase } from '@/lib/supabase';
import { useAuth } from '@/providers/AuthProvider';

export default function LoginScreen() {
  const { configured } = useAuth();
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [mode, setMode] = useState<'login' | 'signup'>('login');
  const [loading, setLoading] = useState(false);
  const [message, setMessage] = useState('');

  async function submit() {
    if (!configured) {
      setMessage('请先在 .env 中配置 Supabase。');
      return;
    }
    if (!email.trim() || password.length < 6) {
      setMessage('请输入邮箱，密码至少 6 位。');
      return;
    }
    setLoading(true);
    setMessage('');
    try {
      const result = mode === 'login'
        ? await supabase.auth.signInWithPassword({ email: email.trim(), password })
        : await supabase.auth.signUp({ email: email.trim(), password });
      if (result.error) {
        const safeMessage = result.error.message.includes('Invalid login credentials')
          ? '邮箱或密码不正确。'
          : result.error.message.includes('User already registered')
            ? '这个邮箱已经注册，可以直接登录。'
            : result.error.message.includes('Email not confirmed')
              ? '请先到邮箱完成确认。'
              : '暂时无法完成，请稍后再试。';
        setMessage(safeMessage);
      } else if (mode === 'signup' && !result.data.session) {
        setMessage('注册成功，请到邮箱完成确认后登录。');
      }
    } catch {
      setMessage('网络连接不稳定，请稍后再试。');
    } finally {
      setLoading(false);
    }
  }

  return (
    <Screen>
      <KeyboardAvoidingView style={styles.page} behavior={Platform.OS === 'ios' ? 'padding' : undefined}>
        <View style={styles.rings} accessible={false}>
          <View style={styles.ringLarge} />
          <View style={styles.ringSmall} />
        </View>
        <View>
          <Text style={styles.brand}>EchoTrace</Text>
          <Text style={styles.title}>一个会慢慢记住你的树洞</Text>
          <Text style={styles.subtitle}>先说下来，理解可以晚一点发生。</Text>
        </View>
        <View style={styles.form}>
          <TextInput
            autoCapitalize="none"
            autoComplete="email"
            keyboardType="email-address"
            placeholder="邮箱"
            placeholderTextColor={colors.muted}
            value={email}
            onChangeText={setEmail}
            style={styles.input}
          />
          <TextInput
            autoCapitalize="none"
            autoComplete={mode === 'login' ? 'current-password' : 'new-password'}
            placeholder="密码"
            placeholderTextColor={colors.muted}
            secureTextEntry
            value={password}
            onChangeText={setPassword}
            style={styles.input}
          />
          {message ? <Text style={styles.message}>{message}</Text> : null}
          <Pressable onPress={submit} disabled={loading} style={({ pressed }) => [styles.primary, pressed && styles.pressed]}>
            <Text style={styles.primaryText}>{loading ? '请稍候…' : mode === 'login' ? '登录' : '注册'}</Text>
          </Pressable>
          <Pressable onPress={() => { setMode(mode === 'login' ? 'signup' : 'login'); setMessage(''); }}>
            <Text style={styles.switchText}>{mode === 'login' ? '还没有账号？注册' : '已经有账号？登录'}</Text>
          </Pressable>
        </View>
      </KeyboardAvoidingView>
    </Screen>
  );
}

const styles = StyleSheet.create({
  page: { flex: 1, justifyContent: 'space-between', paddingVertical: 48 },
  rings: { position: 'absolute', top: 42, right: 0, width: 140, height: 140 },
  ringLarge: { position: 'absolute', inset: 0, borderRadius: 70, borderWidth: 1, borderColor: colors.line },
  ringSmall: { position: 'absolute', top: 28, left: 28, width: 84, height: 84, borderRadius: 42, borderWidth: 1, borderColor: colors.soft },
  brand: { color: colors.accent, fontSize: 15, letterSpacing: 1.4, marginBottom: spacing.lg },
  title: { color: colors.text, fontSize: 28, lineHeight: 40, fontWeight: '600', maxWidth: 390 },
  subtitle: { color: colors.muted, fontSize: 15, marginTop: 12 },
  form: { gap: spacing.md },
  input: { minHeight: 54, borderRadius: radii.medium, backgroundColor: colors.surface, borderWidth: 1, borderColor: colors.line, color: colors.text, paddingHorizontal: 16, fontSize: 16 },
  message: { color: colors.danger, fontSize: 13, lineHeight: 20 },
  primary: { minHeight: 52, borderRadius: radii.medium, backgroundColor: colors.accent, alignItems: 'center', justifyContent: 'center' },
  primaryText: { color: colors.white, fontSize: 16, fontWeight: '600' },
  switchText: { color: colors.accent, textAlign: 'center', fontSize: 14, paddingVertical: 8 },
  pressed: { opacity: 0.75 },
});
